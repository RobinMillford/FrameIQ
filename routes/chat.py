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

import json
import logging
import queue
import re
import threading
from datetime import datetime, timezone

from flask import Blueprint, render_template, request, jsonify, Response, current_app
from flask_login import login_required, current_user
from langchain_core.messages import AIMessage, HumanMessage, BaseMessage

from extensions import limiter
from models import (
    db, User, ChatConversation, ChatMessage, UserChatDailyUsage, UserChatMemory,
)
from src.agents.graph import get_agent_graph, submit_to_chat_loop
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


def _preflight_checkpointer(graph, timeout=10):
    """Verify the checkpointer is alive BEFORE the SSE Response is created.

    Runs checkpointer.setup() ON the chat loop (the only loop the saver
    may be used from) and blocks for the result. Graphs without a
    checkpointer (test doubles) pass trivially.

    Returns True if streaming may start, False if the caller must return
    HTTP 503 instead.
    """
    checkpointer = getattr(graph, "checkpointer", None)
    setup = getattr(checkpointer, "setup", None)
    if setup is None:
        return True

    async def _run_setup():
        await checkpointer.setup()

    try:
        submit_to_chat_loop(_run_setup()).result(timeout=timeout)
        return True
    except Exception:
        logger.error("Chat checkpointer preflight failed", exc_info=True)
        return False


async def _produce_stream(out, graph, initial_state, config, cancel):
    """Producer coroutine. Runs EXCLUSIVELY on the chat loop.

    Iterates graph.astream_events() on the same loop the AsyncSqliteSaver
    was constructed on, normalizes events, and pushes them into a
    thread-safe queue for the sync SSE consumer. The async generator is
    always aclosed on this same loop — no cross-loop cleanup exists.

    Queue protocol: ("event", ui_event) | ("final_state", state|None) |
    ("error", message) | ("done", None). "done" is always last.
    """
    seen_tools = set()
    final_state = None
    agen = graph.astream_events(initial_state, config, version="v2")
    try:
        async for event in agen:
            if cancel is not None and cancel.is_set():
                break
            if event["event"] == "on_chain_end" and event.get("name") == "LangGraph":
                outputs = event.get("data", {}).get("output")
                if isinstance(outputs, dict):
                    final_state = outputs
                continue

            ui_event = _event_to_ui_event(event, seen_tools)
            if ui_event:
                out.put(("event", ui_event))
    except Exception:
        # Failure after streaming may already have started: the consumer
        # turns this marker into a structured SSE error event (HTTP status
        # can no longer change once headers are committed).
        logger.error("Streaming chat error", exc_info=True)
        out.put(("error", "Generation failed"))
    else:
        out.put(("final_state", final_state))
    finally:
        try:
            await agen.aclose()
        except Exception:
            pass
        out.put(("done", None))


def _generate(
    initial_state, config, conversation_id,
    app, graph,
    cancel_token=None,
):
    """Bridge the chat-loop event stream to SSE.

    Eagerly submits the producer coroutine to the chat loop (raising
    BEFORE the Flask Response is created on failure), then returns a
    sync generator that yields SSE chunks. No per-request event loop is
    created here: the AsyncSqliteSaver is only ever touched on the chat
    loop, and the request thread only blocks on a thread-safe queue.

    Runs outside the request context by design — everything the stream
    needs (initial state, config, conversation_id) is captured before
    the Response starts.
    """
    out = queue.Queue()
    try:
        submit_to_chat_loop(
            _produce_stream(out, graph, initial_state, config, cancel_token)
        )
    except Exception:
        logger.error("Chat stream submission failed", exc_info=True)
        raise

    def _consume():
        try:
            while True:
                kind, payload = out.get()
                if kind == "event":
                    yield _sse(payload)
                elif kind == "final_state":
                    if payload:
                        with app.app_context():
                            _save_assistant_message(conversation_id, payload)
                        yield _sse(_build_final_event(payload))
                    else:
                        yield _sse({"type": "error", "error": "No response generated"})
                elif kind == "error":
                    yield _sse({"type": "error", "error": payload})
                elif kind == "done":
                    return
        except GeneratorExit:
            # WSGI server stopped iterating (client disconnected): stop
            # burning LLM tokens for a conversation nobody listens to.
            if cancel_token is not None:
                cancel_token.set()
            raise
        except Exception:
            logger.error("Chat generation failed during streaming", exc_info=True)
            yield _sse({"type": "error", "error": "Generation failed"})
        finally:
            if cancel_token is not None:
                cancel_token.set()

    return _consume()


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

    # PREFLIGHT — before any side effect and before the Response exists:
    # the graph and its checkpointer must be verified ALIVE. Any failure
    # here returns HTTP 503 with JSON (headers not yet committed).
    graph = get_agent_graph()
    if graph is None:
        logger.error("Chat agent graph is not available; returning 503")
        return jsonify({"error": "Chat service temporarily unavailable"}), 503
    if not _preflight_checkpointer(graph):
        logger.error("Chat checkpointer is not available; returning 503")
        return jsonify({"error": "Chat service temporarily unavailable"}), 503

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
    initial_state = _build_initial_state(
        user_message, session_id, user_context, conversation_messages,
    )
    # Each stream invocation gets its own LangGraph thread_id so that
    # concurrent streams for the same conversation do not write checkpoints
    # on top of each other. The conversation_id itself stays stable so the
    # frontend can keep the same conversation list entry. Conversation
    # continuity comes from the ChatMessage rows passed in as initial
    # state, not from resuming a shared checkpoint thread.
    config = {
        "configurable": {
            "thread_id": f"{session_id}_{datetime.now(timezone.utc).timestamp()}",
        },
        "recursion_limit": 20,
    }

    # threading.Event (not asyncio.Event): set from the WSGI consumer
    # thread, polled on the chat loop thread. Only is_set()/set() are
    # used, so no loop binding is involved on either side.
    cancel_token = threading.Event()
    try:
        gen = _generate(
            initial_state, config, conversation.id,
            app, graph, cancel_token=cancel_token,
        )
    except Exception:
        # Submission to the chat loop failed before the Response was
        # created, so HTTP 503 with JSON is still possible here.
        logger.error("Chat request failed before streaming", exc_info=True)
        return jsonify({"error": "Chat service temporarily unavailable"}), 503

    return Response(
        gen,
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache", "X-Accel-Buffering": "no",
            "X-Chat-Remaining": str(remaining),
            "X-Chat-Conversation-ID": str(conversation.id),
        },
    )
