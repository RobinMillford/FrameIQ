"""
Chat route — SSE streaming endpoint backed by the LangGraph agent.

Uses graph.astream_events() for token-level streaming of the final response,
plus tool-call progress events.

SSE event types:
    token     — a chunk of the final reply text
    tool_call — agent used a tool / node progress (label + input summary)
    final     — end of stream + optional movie/TV poster metadata
    error     — unrecoverable failure
"""

import asyncio
import json
import logging

from flask import Blueprint, render_template, request, jsonify, Response
from flask_login import login_required, current_user
from langchain_core.messages import AIMessage

from extensions import limiter
from src.agents.graph import get_agent_graph
from src.api.agent_service import _build_initial_state, _build_user_context

logger = logging.getLogger(__name__)

chat = Blueprint("chat", __name__)

_TOOL_LABELS = {
    "search_tmdb": "Looking up title on TMDb…",
    "search_tmdb_person": "Fetching filmography…",
    "discover_movies": "Discovering movies…",
    "discover_tv": "Discovering TV shows…",
    "get_similar_movies": "Finding similar titles…",
    "search_tmdb_trending": "Checking trending…",
    "my_history": "Checking your watch history…",
}

_NODE_LABELS = {
    "supervisor": "Understanding your question…",
    "enricher": "Fetching posters…",
}

# Only stream tokens produced by the final-response model (tagged in nodes.py),
# not internal calls (retriever tool decisions, enricher extraction).
_FINAL_RESPONSE_TAG = "final_response"


def _sse(data: dict) -> str:
    return f"data: {json.dumps(data)}\n\n"


def _event_to_ui_event(event, seen_tools):
    """Map one astream_events event to a UI SSE dict, or None to skip."""
    kind = event["event"]

    if kind == "on_chat_model_stream":
        if _FINAL_RESPONSE_TAG in event.get("tags", []):
            content = getattr(event["data"]["chunk"], "content", "")
            if content:
                return {"type": "token", "content": content}
        return None

    if kind == "on_tool_start":
        tool = event.get("name", "")
        if tool in _TOOL_LABELS and tool not in seen_tools:
            seen_tools.add(tool)
            return {"type": "tool_call", "tool": tool,
                    "label": _TOOL_LABELS[tool], "input": ""}
        return None

    if kind == "on_chain_start":
        node = event.get("name", "")
        if node in _NODE_LABELS:
            return {"type": "tool_call", "tool": node,
                    "label": _NODE_LABELS[node], "input": ""}
        return None

    return None


async def _astream_events(user_message, session_id, user_context):
    """Async generator of normalized UI events from the graph."""
    graph = get_agent_graph()
    config = {"configurable": {"thread_id": session_id}, "recursion_limit": 20}
    final_state = None
    seen_tools = set()

    async for event in graph.astream_events(
        _build_initial_state(user_message, session_id, user_context),
        config, version="v2"
    ):
        if event["event"] == "on_chain_end" and event.get("name") == "LangGraph":
            outputs = event.get("data", {}).get("output")
            if isinstance(outputs, dict):
                final_state = outputs
            continue

        ui_event = _event_to_ui_event(event, seen_tools)
        if ui_event:
            yield ui_event

    yield {"_final_state": final_state}


def _generate(user_message, session_id, user_context):
    """Sync generator bridging the async event stream to SSE.

    Runs outside the request context by design — everything the stream
    needs (user personalization) is captured before the Response starts.
    """
    try:
        loop = asyncio.new_event_loop()
        agen = _astream_events(user_message, session_id, user_context)
        try:
            while True:
                try:
                    evt = loop.run_until_complete(agen.__anext__())
                except StopAsyncIteration:
                    break

                if "_final_state" in evt:
                    final_state = evt["_final_state"]
                    if final_state:
                        yield _sse(_build_final_event(final_state))
                    else:
                        yield _sse({"type": "error", "error": "No response generated"})
                    continue

                yield _sse(evt)
        finally:
            try:
                loop.run_until_complete(agen.aclose())
            except Exception:
                pass
            try:
                loop.run_until_complete(loop.shutdown_asyncgens())
            except Exception:
                pass
            loop.close()

    except Exception:
        logger.error("Streaming chat error", exc_info=True)
        yield _sse({"type": "error", "error": "Internal server error"})


def _message_text(m) -> str:
    """Normalize message content — some models return a list of blocks."""
    content = getattr(m, "content", "") or ""
    if isinstance(content, list):
        return "".join(str(part) for part in content)
    return content


def _build_final_event(final_state) -> dict:
    messages = final_state.get("messages", [])
    reply = next(
        (_message_text(m) for m in reversed(messages)
         if isinstance(m, AIMessage) and _message_text(m)),
        "",
    )
    event: dict = {"type": "final", "reply": reply}
    meta = final_state.get("final_response_metadata", {})
    if meta.get("movies"):
        event["movies"] = meta["movies"]
    if meta.get("tv_shows"):
        event["tv_shows"] = meta["tv_shows"]
    return event


@chat.route("/chat")
@login_required
def chat_page():
    return render_template("chat.html")


@chat.route("/chat_api", methods=["POST"])
@login_required
@limiter.limit("20 per minute; 100 per hour")
def chat_api():
    """SSE streaming chat — yields tool_call / token / final events."""
    user_message = request.json.get("message")
    if not user_message:
        return jsonify({"error": "Message is required"}), 400

    session_id = f"user_{current_user.id}"
    # Capture personalization inside the request context — the stream
    # generator itself runs without one.
    user_context = _build_user_context(session_id)

    return Response(
        _generate(user_message, session_id, user_context),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
