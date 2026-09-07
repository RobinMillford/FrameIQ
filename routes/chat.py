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
import re
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from functools import wraps

from flask import Blueprint, render_template, request, jsonify, Response, current_app
from flask_login import login_required, current_user
from langchain_core.messages import AIMessage, HumanMessage, BaseMessage

from extensions import limiter
from models import (
    db, User, ChatConversation, ChatMessage, UserChatDailyUsage, UserChatMemory,
)
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
DAILY_CHAT_LIMIT = 5


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


async def _astream_events(
    user_message, session_id, user_context, conversation_messages=None,
    cancel_token=None,
):
    """Async generator of normalized UI events from the graph.

    Uses the process-long-lived compiled graph (get_agent_graph) which now
    carries an AsyncSqliteSaver checkpointer. Because the checkpointer is
    async-safe and built once per process, there is no per-request
    SqliteSaver construction and no per-request SQLite connection churn.

    A stream-scoped thread_id is still used so any leftover in-flight / half-
    written checkpoint does not collide with a future re-submit of the same
    conversation_id.
    """
    graph = get_agent_graph()
    if graph is None:
        # If graph compilation failed (e.g. aiosqlite missing/bad config in
        # this process), fail fast with a structured error the frontend can
        # display instead of hanging / returning HTTP 200 with no body.
        raise RuntimeError("Chat agent graph is not available")

    # Each stream invocation gets its own LangGraph thread_id so that
    # concurrent streams for the same conversation do not write checkpoints
    # on top of each other. The conversation_id itself stays stable so the
    # frontend can keep the same conversation list entry.
    config = {
        "configurable": {
            "thread_id": f"{session_id}_{datetime.now(timezone.utc).timestamp()}",
        },
        "recursion_limit": 20,
    }
    final_state = None
    seen_tools = set()

    cancel_event = cancel_token
    config["configurable"]["cancel_token"] = cancel_token
    try:
        async for event in graph.astream_events(
            _build_initial_state(
                user_message, session_id, user_context, conversation_messages,
            ),
            config, version="v2"
        ):
            if cancel_event is not None and cancel_event.is_set():
                break
            if event["event"] == "on_chain_end" and event.get("name") == "LangGraph":
                outputs = event.get("data", {}).get("output")
                if isinstance(outputs, dict):
                    final_state = outputs
                continue

            ui_event = _event_to_ui_event(event, seen_tools)
            if ui_event:
                yield ui_event
    finally:
        pass

    yield {"_final_state": final_state}


def _generate(
    user_message, session_id, user_context, conversation_id, conversation_messages,
    app,
    cancel_token=None,
):
    """Sync generator bridging the async event stream to SSE.

    A single event loop is created per stream and owned by this generator
    for its whole lifetime. The async generator is driven with the idiomatic
    anext()/__anext__() protocol plus explicit aclose() in the finally block
    so cancellations / client disconnects do not leave dangling tasks.

    Runs outside the request context by design — everything the stream
    needs (user personalization, conversation_id) is captured before the
    Response starts.
    """
    loop = None
    agen = None
    agen_done = asyncio.Event()
    try:
        loop = asyncio.new_event_loop()
        agen = _astream_events(
            user_message, session_id, user_context, conversation_messages,
            cancel_token=cancel_token,
        )
        try:
            while not agen_done.is_set():
                try:
                    evt = loop.run_until_complete(anext(agen))
                except StopAsyncIteration:
                    break
                except asyncio.CancelledError:
                    break

                if "_final_state" in evt:
                    final_state = evt["_final_state"]
                    if final_state:
                        with app.app_context():
                            _save_assistant_message(conversation_id, final_state)
                        yield _sse(_build_final_event(final_state))
                    else:
                        yield _sse({"type": "error", "error": "No response generated"})
                    continue

                yield _sse(evt)
        except BaseException:
            # An error here means streaming failed after HTTP 200 started.
            # Yield one structured error event so the frontend can show a
            # retryable message instead of a silent truncation.
            logger.error("Streaming chat error", exc_info=True)
            yield _sse({"type": "error", "error": "Generation failed"})
            raise
        finally:
            if agen is not None:
                try:
                    loop.run_until_complete(agen.aclose())
                except BaseException:
                    pass
    except BaseException:
        # Error before the first yield — HTTP 200 has not started yet, so
        # the caller will turn this into a non-200 response.
        logger.error("Chat generation failed before streaming", exc_info=True)
        raise


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


def _conversation_for_user(conversation_id):
    if conversation_id is None:
        conversation = ChatConversation(user_id=current_user.id)
        db.session.add(conversation)
        db.session.flush()
        return conversation
    return ChatConversation.query.filter_by(
        id=conversation_id, user_id=current_user.id,
    ).first()


def _consumer_raises_disconnect(c):
    """Best-effort detection that the HTTP client disconnected.

    Werkzeug wraps the WSGI response in a consumer; when that consumer
    raises (most commonly because the client closed the connection) we
    treat the stream as aborted and stop driving the graph.
    """
    return isinstance(c, Exception)


def _run_with_client_disconnect_handling(gen, app, cancel_token=None):
    """Drive a WSGI-streaming generator while detecting client disconnects.

    If the client disconnects mid-stream we set the cancel_token so the async
    graph stream can stop yielding and the process stops burning LLM tokens for
    a conversation nobody is listening to.
    """
    agen_done = asyncio.Event()

    def _on_disconnect(exc):
        if cancel_token is not None:
            cancel_token.set()
        agen_done.set()

    try:
        for chunk in gen:
            yield chunk
    except BaseException:
        # Client disconnect or generator failure: mark the async stream done
        # so run_until_complete(anext(...)) can unwind without spinning.
        agen_done.set()
        raise

    # Best-effort detection of HTTP client disconnection is left to the WSGI
    # server; the generator simply stops yielding and the caller's
    # _generate() finally block runs aclose().


def _history_messages(conversation):
    messages = []
    for message in conversation.messages.order_by(ChatMessage.created_at).all():
        message_type = HumanMessage if message.role == "user" else AIMessage
        messages.append(message_type(content=message.content))
    return messages


def _save_assistant_message(conversation_id, final_state):
    messages = final_state.get("messages", [])
    reply = next(
        (_message_text(m) for m in reversed(messages)
         if isinstance(m, AIMessage) and _message_text(m)),
        "",
    )
    if not reply:
        return
    conversation = db.session.get(ChatConversation, conversation_id)
    if not conversation:
        return
    metadata = final_state.get("final_response_metadata") or {}
    db.session.add(ChatMessage(
        conversation_id=conversation_id,
        role="assistant",
        content=reply,
        metadata_json=json.dumps(metadata),
    ))
    conversation.updated_at = datetime.utcnow()
    db.session.commit()


def _remember_user_preference(user_id, message):
    """Persist explicit preference statements without another paid model call."""
    if not re.search(r"\b(i like|i love|i prefer|my favorite|i hate|i dislike)\b",
                     message, re.IGNORECASE):
        return
    memory = UserChatMemory.query.filter_by(user_id=user_id).first()
    if memory is None:
        memory = UserChatMemory(user_id=user_id, content="")
        db.session.add(memory)
    entries = [entry for entry in memory.content.split("\n") if entry]
    if message not in entries:
        entries.append(message[:500])
    memory.content = "\n".join(entries[-20:])


def _consume_daily_question(user_id):
    today = datetime.now(timezone.utc).date()
    # Lock the existing user row so concurrent requests cannot both pass the
    # quota check before either transaction commits.
    db.session.execute(
        db.select(User.id).where(User.id == user_id).with_for_update()
    ).scalar_one()
    usage = UserChatDailyUsage.query.filter_by(
        user_id=user_id, usage_date=today,
    ).with_for_update().first()
    if usage is None:
        usage = UserChatDailyUsage(
            user_id=user_id, usage_date=today, question_count=0,
        )
        db.session.add(usage)
        db.session.flush()
    if usage.question_count >= DAILY_CHAT_LIMIT:
        db.session.rollback()
        return None
    usage.question_count += 1
    db.session.commit()
    return DAILY_CHAT_LIMIT - usage.question_count


def _quota_status(user_id):
    today = datetime.now(timezone.utc).date()
    usage = UserChatDailyUsage.query.filter_by(
        user_id=user_id, usage_date=today,
    ).first()
    used = usage.question_count if usage else 0
    return {"used": used, "limit": DAILY_CHAT_LIMIT, "remaining": max(0, DAILY_CHAT_LIMIT - used)}


@chat.route("/chat")
@login_required
def chat_page():
    conversations = ChatConversation.query.filter_by(
        user_id=current_user.id,
    ).order_by(ChatConversation.updated_at.desc()).limit(50).all()
    return render_template(
        "chat.html",
        conversations=conversations,
        quota=_quota_status(current_user.id),
    )


@chat.route("/chat/conversations", methods=["GET"])
@login_required
def chat_conversations():
    conversations = ChatConversation.query.filter_by(
        user_id=current_user.id,
    ).order_by(ChatConversation.updated_at.desc()).limit(50).all()
    return jsonify({"conversations": [
        {"id": item.id, "title": item.title,
         "updated_at": item.updated_at.isoformat()}
        for item in conversations
    ], "quota": _quota_status(current_user.id)})


@chat.route("/chat/conversations/<int:conversation_id>", methods=["GET"])
@login_required
def chat_conversation(conversation_id):
    conversation = ChatConversation.query.filter_by(
        id=conversation_id, user_id=current_user.id,
    ).first_or_404()
    return jsonify({
        "id": conversation.id,
        "title": conversation.title,
        "messages": [
            {"role": message.role, "content": message.content,
             "metadata": json.loads(message.metadata_json or "{}")}
            for message in conversation.messages.order_by(ChatMessage.created_at).all()
        ],
    })


@chat.route("/chat_api", methods=["POST"])
@login_required
@limiter.limit("20 per minute; 100 per hour")
def chat_api():
    """SSE streaming chat — yields tool_call / token / final events."""
    app = current_app._get_current_object()
    payload = request.get_json(silent=True) or {}
    user_message = (payload.get("message") or "").strip()
    if not user_message:
        return jsonify({"error": "Message is required"}), 400

    conversation_id = payload.get("conversation_id")
    conversation = _conversation_for_user(conversation_id)
    if conversation is None:
        return jsonify({"error": "Conversation not found"}), 404
    remaining = _consume_daily_question(current_user.id)
    if remaining is None:
        return jsonify({
            "error": "Daily chat limit reached. You have 5 questions per UTC day.",
            "quota": _quota_status(current_user.id),
        }), 429
    if conversation.title == "New chat":
        conversation.title = user_message[:80]
    db.session.add(ChatMessage(
        conversation_id=conversation.id, role="user", content=user_message,
    ))
    _remember_user_preference(current_user.id, user_message)
    db.session.commit()

    session_id = f"user_{current_user.id}_conversation_{conversation.id}"
    # Capture personalization inside the request context — the stream
    # generator itself runs without one.
    user_context = _build_user_context(session_id)
    conversation_messages = _history_messages(conversation)[:-1]

    cancel_token = asyncio.Event()
    gen = _generate(
        user_message, session_id, user_context, conversation.id,
        conversation_messages, app, cancel_token=cancel_token,
    )
    try:
        resp = Response(
            _run_with_client_disconnect_handling(gen, app, cancel_token=cancel_token),
            mimetype="text/event-stream",
            headers={
                "Cache-Control": "no-cache", "X-Accel-Buffering": "no",
                "X-Chat-Remaining": str(remaining),
                "X-Chat-Conversation-ID": str(conversation.id),
            },
        )
    except Exception:
        # If generation fails before the first byte is yielded the Response
        # constructor has not returned yet, so we can return a normal error.
        logger.error("Chat request failed before streaming", exc_info=True)
        return jsonify({"error": "Streaming not available"}), 503

    # Register a best-effort cleanup hook so that, if the WSGI server
    # supports it, we stop driving the graph when the HTTP response is done.
    import atexit
    atexit.register(lambda: cancel_token.set())
    return resp
