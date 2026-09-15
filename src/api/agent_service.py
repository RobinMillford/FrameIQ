"""
Agent service layer — orchestrates the LangGraph workflow.

Key improvements vs. original:
- _build_user_context() injects personalisation from watch history / ratings.
- Long-term taste now comes from the canonical persisted TasteProfile
  (api/taste_profile.py — Feature #6 Phase 13); recent ratings, TV tracking,
  watchlist size and continue-watching remain as CURRENT-context signals.
- initial state includes user_context on every invocation.
- Streaming generator scoping bug fixed (messages variable captured correctly).
- session_id always derived from user ID, never from IP.
"""

import logging
from typing import Dict, Any, Generator, Optional, Sequence

from langchain_core.messages import BaseMessage, HumanMessage

from src.agents.graph import get_agent_graph
from src.agents.state import GraphState
from src.agents.error_handling import retry_on_error, get_fallback_response
from src.agents.memory import update_conversation_metadata, get_conversation_context
from src.agents.monitoring import track_performance, log_agent_decision, get_performance_metrics

logger = logging.getLogger(__name__)


# ── Personalisation ───────────────────────────────────────────────────────────

def _taste_profile_lines(summary) -> list:
    """Bounded signal lines from a describe_profile() summary (pure)."""
    lines = ["- strongest genres: " + ", ".join(
        g['genre'] for g in summary['top_positive_genres'])]

    negatives = summary['top_negative_genres']
    if negatives:
        lines.append("- genres they tend to steer away from: "
                     + ", ".join(g['genre'] for g in negatives))

    directors = summary['director_affinity'] or {}
    if directors:
        top_dirs = sorted(directors.items(), key=lambda kv: (-kv[1], kv[0]))[:3]
        lines.append("- favored directors: "
                     + ", ".join(name for name, _ in top_dirs))

    decades = summary['top_decades']
    if decades:
        lines.append("- preferred eras: " + ", ".join(
            d['decade'] for d in decades))

    runtime = summary['runtime_pref'] or {}
    p25, p75 = runtime.get('p25'), runtime.get('p75')
    if p25 and p75 and p75 > p25:
        lines.append(f"- runtime preference: about {p25:.0f}-{p75:.0f} min")

    media_pref = summary['media_type_pref'] or {}
    if media_pref:
        parts = [f"{name} ({share:.0%})"
                 for name, share in sorted(media_pref.items(),
                                           key=lambda kv: (-kv[1], kv[0]))]
        lines.append("- media preference: " + ", ".join(parts))
    return lines


def _format_taste_profile(profile) -> Optional[str]:
    """Deterministic, bounded taste block from the canonical TasteProfile.

    Pure formatting — no DB, no network, no LLM. Uses describe_profile()
    (the canonical analysis helper) so no second aggregation lives here.
    Wording is calibrated: negative evidence is phrased as a tendency, not
    an absolute; limited-confidence profiles are explicitly labelled as
    hints. Returns None for missing/empty profiles (cold-start users get
    NO taste section at all — CineBot must never claim a profile exists).
    """
    if profile is None:
        return None
    try:
        from api.taste_profile import describe_profile
        from api.for_you import FULL_PERSONALIZED_MIN_CONFIDENCE

        summary = describe_profile(profile)
        if not summary['top_positive_genres']:
            return None  # nothing meaningful to say about long-term taste

        lines = _taste_profile_lines(summary)

        confidence = summary['confidence'] or 0.0
        titles = summary['distinct_title_count'] or 0
        if confidence >= FULL_PERSONALIZED_MIN_CONFIDENCE:
            lines.append(
                f"- taste confidence: strong (based on {titles} titles)")
        else:
            lines.append(
                "- taste confidence: limited evidence — treat these as "
                "hints, not certainties")

        return "[TASTE PROFILE]\n" + "\n".join(lines) + "\n[/TASTE PROFILE]"
    except Exception as e:
        logger.debug("Could not format taste profile: %s", e)
        return None


def _chat_memory_section(user_id: int) -> list:
    """Remembered chat preferences (current context, not a taste model)."""
    from models import UserChatMemory

    memory = UserChatMemory.query.filter_by(user_id=user_id).first()
    if memory and memory.content:
        return [
            "Long-term preferences remembered from chat:\n" + memory.content
        ]
    return []


def _recent_ratings_section(user_id: int) -> list:
    """Recent ratings — recent events, deliberately not a taste model."""
    from models import Review

    reviews = (
        Review.query.filter_by(user_id=user_id)
        .order_by(Review.created_at.desc()).limit(10).all()
    )
    if not reviews:
        return []
    lines = []
    for r in reviews:
        try:
            title = r.media.title if r.media else "Unknown"
            score = f"{r.rating * 2:.1f}/10" if r.rating else "unrated"
            lines.append(f"  - {title} ({r.media_type}): {score}")
        except Exception:
            continue
    return ["Recent ratings (use to personalise):\n" + "\n".join(lines)]


def _tv_tracking_section(user_id: int) -> list:
    """TV shows currently being tracked (current context)."""
    from models import TVShowProgress

    tracking = (
        TVShowProgress.query.filter_by(user_id=user_id)
        .filter(TVShowProgress.status.in_(["watching", "plan_to_watch"]))
        .limit(8).all()
    )
    if not tracking:
        return []
    lines = []
    for t in tracking:
        pct = t.calculate_progress_percentage()
        lines.append(
            f"  - show_id {t.show_id}: {t.watched_episodes}/"
            f"{t.total_episodes} eps ({pct}%) — {t.status}"
        )
    return ["TV shows they are tracking:\n" + "\n".join(lines)]


def _watch_progress_section(user_id: int) -> list:
    """Continue-watching progress (current context)."""
    from models import WatchProgress

    progress = (
        WatchProgress.query.filter_by(user_id=user_id)
        .order_by(WatchProgress.updated_at.desc()).limit(5).all()
    )
    if not progress:
        return []
    lines = []
    for p in progress:
        label = p.title or f"{p.media_type} {p.tmdb_id}"
        lines.append(f"  - {label}: {p.progress_pct}% watched")
    return [
        "Recently streamed (partially watched):\n" + "\n".join(lines)
    ]


def _build_user_context(session_id: str) -> str:
    """
    Build a personalisation context from the user's FrameIQ data.

    Two clearly separated layers:

    LONG-TERM TASTE — the canonical persisted TasteProfile
    (api/taste_profile.py, computed nightly). Loaded at most once per
    context build via get_profile(); never recomputed here, never read
    from RecommendationFeedback directly.

    CURRENT CONTEXT — recent ratings, TV tracking, watchlist size,
    continue-watching progress and remembered chat preferences. These are
    recent-state signals, not a second taste model (Phase 13 removed the
    old duplicate genre-Counter aggregation).

    Returns an empty string if the user has no history or on any error.
    """
    if not session_id.startswith("user_"):
        return ""
    try:
        user_id = int(session_id.split("_")[1])
    except (IndexError, ValueError):
        return ""

    try:
        from flask import has_app_context
        if not has_app_context():
            return ""

        from models import db, user_watchlist

        sections = []

        sections.extend(_chat_memory_section(user_id))

        # ── Long-term taste (canonical persisted TasteProfile) ──
        from api.taste_profile import get_profile
        taste_block = _format_taste_profile(get_profile(user_id))
        if taste_block:
            sections.append(taste_block)

        # ── Current context (recent events, not a taste model) ──
        sections.extend(_recent_ratings_section(user_id))
        sections.extend(_tv_tracking_section(user_id))

        # ── Watchlist size (count query: SELECT .rowcount is unreliable) ──
        wl_count = db.session.scalar(
            db.select(db.func.count()).select_from(user_watchlist)
            .where(user_watchlist.c.user_id == user_id))
        if wl_count:
            sections.append(f"Watchlist: {wl_count} titles saved.")

        sections.extend(_watch_progress_section(user_id))

        if not sections:
            return ""
        return "\n\n".join(sections)
    except Exception as e:
        logger.debug("Could not build user context: %s", e)
        return ""


def _build_initial_state(
    user_message: str,
    session_id: str,
    user_context: Optional[str] = None,
    conversation_messages: Optional[Sequence[BaseMessage]] = None,
) -> GraphState:
    """Construct a fresh GraphState for a new invocation."""
    ctx = user_context if user_context is not None else _build_user_context(session_id)
    user_id = None
    if session_id.startswith("user_"):
        try:
            user_id = int(session_id.split("_")[1])
        except (IndexError, ValueError):
            user_id = None
    return {
        "messages": [
            *(conversation_messages or []),
            HumanMessage(content=user_message),
        ],
        "user_intent": None,
        "next_step": None,
        "entities": {},
        "user_id": user_id,
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
