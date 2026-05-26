"""
Chat route — SSE streaming endpoint backed by the LangGraph agent.

Uses synchronous graph.stream() (node-level updates).
Tool calls are surfaced from retrieved_context after the retriever node completes.

SSE event types:
    tool_call — agent used a tool (label + input summary)
    final     — full reply + optional movie/TV poster metadata
    error     — unrecoverable failure
"""

import json
import logging

from flask import Blueprint, render_template, request, jsonify, Response, stream_with_context
from flask_login import login_required, current_user
from langchain_core.messages import AIMessage

from src.agents.graph import get_agent_graph
from src.api.agent_service import _build_initial_state

logger = logging.getLogger(__name__)

chat = Blueprint("chat", __name__)

_TOOL_LABELS = {
    "search_vector_db": "Searching vibe database…",
    "search_tmdb": "Looking up title on TMDb…",
    "search_tmdb_person": "Fetching filmography…",
    "discover_movies": "Discovering movies…",
    "discover_tv": "Discovering TV shows…",
    "get_similar_movies": "Finding similar titles…",
    "search_tmdb_trending": "Checking trending…",
}


@chat.route("/chat")
@login_required
def chat_page():
    return render_template("chat.html")


def _node_events(node_name, node_state):
    """Yield SSE dicts for a single graph node update."""
    if node_name == "supervisor":
        intent = node_state.get("user_intent", "")
        label = "Routing to search…" if intent == "search" else "Generating response…"
        yield {"type": "tool_call", "tool": "supervisor", "label": label, "input": ""}

    elif node_name == "retriever":
        for ctx in node_state.get("retrieved_context", []):
            tool = ctx.get("tool", "unknown")
            yield {"type": "tool_call", "tool": tool,
                   "label": _TOOL_LABELS.get(tool, f"Used {tool}…"), "input": ""}

    elif node_name == "enricher":
        yield {"type": "tool_call", "tool": "enricher",
               "label": "Fetching posters…", "input": ""}


def _build_final_event(final_state) -> dict:
    messages = final_state.get("messages", [])
    reply = next(
        (m.content for m in reversed(messages)
         if isinstance(m, AIMessage) and m.content),
        "",
    )
    event: dict = {"type": "final", "reply": reply}
    meta = final_state.get("final_response_metadata", {})
    if meta.get("movies"):
        event["movies"] = meta["movies"]
    if meta.get("tv_shows"):
        event["tv_shows"] = meta["tv_shows"]
    return event


def _generate(user_message, session_id):
    try:
        graph = get_agent_graph()
        config = {"configurable": {"thread_id": session_id}, "recursion_limit": 20}
        final_state = None

        for state_update in graph.stream(_build_initial_state(user_message, session_id), config):
            for node_name, node_state in state_update.items():
                for evt in _node_events(node_name, node_state):
                    yield _sse(evt)
                final_state = node_state

        if not final_state:
            yield _sse({"type": "error", "error": "No response generated"})
            return

        yield _sse(_build_final_event(final_state))

    except Exception:
        logger.error("Streaming chat error", exc_info=True)
        yield _sse({"type": "error", "error": "Internal server error"})


@chat.route("/chat_api", methods=["POST"])
@login_required
def chat_api():
    """SSE streaming chat — yields tool_call / final events."""
    user_message = request.json.get("message")
    if not user_message:
        return jsonify({"error": "Message is required"}), 400

    return Response(
        stream_with_context(_generate(user_message, f"user_{current_user.id}")),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _sse(data: dict) -> str:
    return f"data: {json.dumps(data)}\n\n"
