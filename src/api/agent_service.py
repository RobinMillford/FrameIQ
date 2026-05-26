"""
Agent service layer — orchestrates the LangGraph workflow.

Key improvements vs. original:
- _build_user_context() injects personalisation from watch history / ratings.
- initial state includes user_context on every invocation.
- Streaming generator scoping bug fixed (messages variable captured correctly).
- session_id always derived from user ID, never from IP.
"""

import logging
from typing import Dict, Any, Generator, Optional

from langchain_core.messages import HumanMessage

from src.agents.graph import get_agent_graph
from src.agents.state import GraphState
from src.agents.error_handling import retry_on_error, get_fallback_response
from src.agents.memory import update_conversation_metadata, get_conversation_context
from src.agents.monitoring import track_performance, log_agent_decision, get_performance_metrics

logger = logging.getLogger(__name__)


# ── Personalisation ───────────────────────────────────────────────────────────

def _build_user_context(session_id: str) -> str:
    """
    Build a personalisation string from the user's watch history and ratings.

    Queries the DB for the 8 most-recently rated items so the agent can
    tailor recommendations without exposing raw DB objects to the graph.
    Returns an empty string if the user has no history or on any error.
    """
    if not session_id.startswith("user_"):
        return ""
    try:
        user_id = int(session_id.split("_")[1])
    except (IndexError, ValueError):
        return ""

    try:
        from models import Review, db  # noqa: F401 — imported inside fn to avoid circular import
        from flask import has_app_context

        if not has_app_context():
            return ""

        reviews = (
            Review.query
            .filter_by(user_id=user_id)
            .order_by(Review.created_at.desc())
            .limit(8)
            .all()
        )
        if not reviews:
            return ""

        lines = ["User's recent watch history (use to personalise):"]
        for r in reviews:
            try:
                title = r.media.title if r.media else "Unknown"
                # Ratings stored as 0.5–5.0 stars; convert to /10 for LLM clarity
                score = f"{r.rating * 2:.1f}/10" if r.rating else "unrated"
                lines.append(f"  - {title} ({r.media_type}): {score}")
            except Exception:
                continue
        return "\n".join(lines)
    except Exception as e:
        logger.debug("Could not build user context: %s", e)
        return ""


def _build_initial_state(
    user_message: str, session_id: str, user_context: Optional[str] = None
) -> GraphState:
    """Construct a fresh GraphState for a new invocation."""
    ctx = user_context if user_context is not None else _build_user_context(session_id)
    return {
        "messages": [HumanMessage(content=user_message)],
        "user_intent": None,
        "next_step": None,
        "retrieved_context": [],
        "final_response_metadata": {"movies": [], "tv_shows": []},
        "user_context": ctx,
    }


# ── Non-streaming ─────────────────────────────────────────────────────────────

@track_performance
@retry_on_error(max_retries=2, delay=1.0)
def run_agent_chat(user_message: str, session_id: str) -> Dict[str, Any]:
    """
    Run the LangGraph agent workflow synchronously.

    Returns a dict with:
        reply     — AI response text
        movies    — (optional) list of movie metadata dicts
        tv_shows  — (optional) list of TV show metadata dicts
        metadata  — session / routing diagnostics
    """
    graph = get_agent_graph()
    context = get_conversation_context(session_id)
    logger.info("Session context: %s", context)

    initial_state = _build_initial_state(user_message, session_id)
    config = {
        "configurable": {"thread_id": session_id},
        "recursion_limit": 15,
    }

    try:
        final_state = graph.invoke(initial_state, config)

        messages = final_state["messages"]
        final_reply = next(
            (m.content for m in reversed(messages) if getattr(m, "content", None)),
            "",
        )

        update_conversation_metadata(
            session_id,
            message_count=len(messages),
            metadata={
                "last_intent": final_state.get("user_intent"),
                "last_route": final_state.get("next_step"),
            },
        )

        log_agent_decision(
            node_name="supervisor",
            decision=final_state.get("next_step", "unknown"),
            reasoning=f"Intent: {final_state.get('user_intent')}",
            metadata={"session_id": session_id},
        )

        response: Dict[str, Any] = {
            "reply": final_reply,
            "metadata": {
                "session_id": session_id,
                "message_count": len(messages),
                "route": final_state.get("next_step"),
                "intent": final_state.get("user_intent"),
            },
        }
        enriched = final_state.get("final_response_metadata", {})
        if enriched.get("movies"):
            response["movies"] = enriched["movies"]
        if enriched.get("tv_shows"):
            response["tv_shows"] = enriched["tv_shows"]
        return response

    except Exception as e:
        logger.error("Agent workflow error: %s", e, exc_info=True)
        error_type = "rate_limit" if "rate" in str(e).lower() or "429" in str(e) else (
            "timeout" if "timeout" in str(e).lower() else "llm_error"
        )
        return {
            "reply": get_fallback_response(error_type, user_message),
            "error": str(e),
            "metadata": {"session_id": session_id, "error_type": error_type},
        }


# ── Streaming ─────────────────────────────────────────────────────────────────

def run_agent_chat_streaming(
    user_message: str, session_id: str
) -> Generator[Dict[str, Any], None, None]:
    """
    Stream LangGraph node-level updates for real-time progress UI.

    Yields one dict per node completion:
        node, message, intent, next_step, metadata

    Note: this is *graph-level* streaming (one event per node), not
    token-level streaming. For true token streaming, migrate to
    graph.astream_events() with on_chat_model_stream filtering.
    """
    graph = get_agent_graph()
    initial_state = _build_initial_state(user_message, session_id)
    config = {
        "configurable": {"thread_id": session_id},
        "recursion_limit": 15,
    }

    final_message_count = 0
    try:
        for state_update in graph.stream(initial_state, config):
            for node_name, node_state in state_update.items():
                node_messages = node_state.get("messages", [])
                latest_message = ""
                if node_messages:
                    last = node_messages[-1]
                    latest_message = getattr(last, "content", "") or ""
                final_message_count = len(node_messages)

                yield {
                    "node": node_name,
                    "message": latest_message,
                    "intent": node_state.get("user_intent"),
                    "next_step": node_state.get("next_step"),
                    "metadata": node_state.get("final_response_metadata", {}),
                }
    except Exception as e:
        logger.error("Streaming error: %s", e, exc_info=True)
        yield {
            "node": "error",
            "message": get_fallback_response("llm_error", user_message),
            "error": str(e),
        }
    finally:
        update_conversation_metadata(session_id, message_count=final_message_count)


def get_agent_metrics() -> Dict[str, Any]:
    """Return accumulated performance metrics for the agent system."""
    return get_performance_metrics()
