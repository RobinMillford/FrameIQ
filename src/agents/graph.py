"""
Main StateGraph construction for the FrameIQ multi-agent system.

This module builds the complete workflow with conditional routing
and persistent (SQLite) conversation checkpointing.

Loop-safety architecture (OPTION A — single dedicated loop):

    One daemon thread per process runs a single asyncio event loop
    ("chat loop") for the lifetime of the process.

    - AsyncSqliteSaver is CONSTRUCTED on the chat loop.
    - The compiled graph is COMPILED on the chat loop.
    - Every async graph call (astream_events / ainvoke) is SUBMITTED
      to the chat loop via asyncio.run_coroutine_threadsafe().
    - Sync graph calls (invoke / stream) are safe from any thread:
      the installed AsyncSqliteSaver (langgraph-checkpoint-sqlite
      2.0.11) routes those through run_coroutine_threadsafe() onto
      the construction loop itself (see _astream_events callers).

    Request/worker threads NEVER drive the saver with their own event
    loop. This is required because AsyncSqliteSaver holds an
    asyncio.Lock created at construction time: uncontended cross-loop
    use accidentally works on Python 3.10+, but contended cross-loop
    use deadlocks (verified empirically — two loops contending on one
    saver lock hang forever). A per-request loop per stream would hit
    exactly that hazard under concurrent chat traffic.

Conversation continuity itself comes from ChatConversation/ChatMessage
DB rows (passed into each invocation as initial state), so each stream
also uses a unique timestamp-scoped thread_id — concurrent streams for
the same conversation never overwrite each other's checkpoints.
"""

import asyncio
import logging
import os
import threading
import time
import traceback

from langgraph.graph import StateGraph, START, END
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from .state import GraphState
from .nodes import (
    supervisor_node,
    retriever_node,
    chat_node,
    enricher_node,
    should_continue,
)

logger = logging.getLogger(__name__)

# Conversation memory survives server restarts. Location is overridable
# for tests (in-memory SQLite via ":memory:" would defeat persistence,
# so a file path is the default).
_CHECKPOINT_DB = os.getenv(
    "CHAT_CHECKPOINT_DB",
    os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__)))), "instance", "chat_memory.db",
    ),
)
os.makedirs(os.path.dirname(_CHECKPOINT_DB), exist_ok=True)

_BUILD_TIMEOUT = 30

_graph_instance = None
# The original build failure (if any) plus its formatted traceback, so
# callers can log the REAL cause instead of a generic message.
_graph_error = None
_graph_traceback = None

_chat_loop = None
_chat_loop_thread = None
_startup_lock = threading.Lock()
_ready = threading.Event()


def _run_loop_forever(loop):
    asyncio.set_event_loop(loop)
    loop.run_forever()


def get_chat_loop():
    """Return the process-wide chat event loop, starting it if needed.

    The loop runs forever on its own daemon thread. It is the ONLY loop
    the AsyncSqliteSaver / compiled graph may ever be used from.
    Thread-safe: safe to call from any request/worker/test thread.
    """
    global _chat_loop, _chat_loop_thread
    if _chat_loop is not None:
        return _chat_loop
    with _startup_lock:
        if _chat_loop is not None:
            return _chat_loop
        loop = asyncio.new_event_loop()
        thread = threading.Thread(
            target=_run_loop_forever, args=(loop,),
            daemon=True, name="frameiq-chat-loop",
        )
        thread.start()
        _chat_loop = loop
        _chat_loop_thread = thread
        return loop


async def _build_async_ckpt_saver():
    """Build an AsyncSqliteSaver bound to a dedicated aiosqlite connection.

    MUST run on the chat loop (see module docstring). The saver captures
    the running loop and an asyncio.Lock at construction; both are only
    ever touched from this same loop afterwards.

    The SQLite connection uses WAL mode and a busy timeout so the
    checkpoint file shared by Gunicorn workers tolerates concurrent
    writers.
    """
    import aiosqlite

    db_conn = await aiosqlite.connect(_CHECKPOINT_DB)
    # Enable WAL + busy timeout once at startup so the checkpoint DB
    # tolerates concurrent writer checkout from multiple workers.
    await db_conn.execute("PRAGMA journal_mode=WAL")
    await db_conn.execute("PRAGMA busy_timeout=5000")
    await db_conn.commit()
    if not hasattr(db_conn, "is_alive"):
        # COMPAT SHIM (verified against installed pins:
        # langgraph-checkpoint-sqlite==2.0.11 with aiosqlite==0.22.1).
        # saver.setup() calls conn.is_alive(), which this aiosqlite
        # release does not provide — without this shim EVERY setup()
        # raises AttributeError and checkpoint persistence silently dies.
        # setup() only uses it as "reconnect if dead", so the faithful
        # semantic is: True while the worker thread holds a live
        # sqlite3 connection.
        def _is_alive(conn=db_conn):
            return conn._connection is not None

        db_conn.is_alive = _is_alive
        logger.warning(
            "aiosqlite %s lacks Connection.is_alive; applied compat shim "
            "for langgraph-checkpoint-sqlite saver",
            getattr(aiosqlite, "__version__", "unknown"),
        )
    saver = AsyncSqliteSaver(db_conn)
    await saver.setup()
    return saver


async def _build_graph_once():
    """Coroutine that compiles the graph. Runs ON the chat loop.

    On failure the original exception + traceback are stored on the
    module so get_agent_graph() can log the real cause. No silent
    in-memory fallback: losing checkpoint persistence silently is worse
    than an explicit, loggable initialization failure (callers turn it
    into HTTP 503 before streaming starts).
    """
    global _graph_instance, _graph_error, _graph_traceback
    if _graph_instance is not None:
        return _graph_instance

    saver = await _build_async_ckpt_saver()

    workflow = StateGraph(GraphState)
    workflow.add_node("supervisor", supervisor_node)
    workflow.add_node("retriever", retriever_node)
    workflow.add_node("chat", chat_node)
    workflow.add_node("enricher", enricher_node)

    workflow.add_edge(START, "supervisor")
    workflow.add_conditional_edges(
        "supervisor",
        should_continue,
        {
            "retriever": "retriever",
            "chat": "chat",
            "__end__": END,
        },
    )

    workflow.add_edge("retriever", "enricher")
    workflow.add_edge("chat", "enricher")
    workflow.add_edge("enricher", END)

    _graph_instance = workflow.compile(checkpointer=saver)
    return _graph_instance


def _build_in_background(loop):
    """Submit the build coroutine to the chat loop; record real errors."""
    global _graph_error, _graph_traceback
    try:
        fut = asyncio.run_coroutine_threadsafe(_build_graph_once(), loop)
        fut.result(timeout=_BUILD_TIMEOUT)
    except Exception as exc:  # noqa: BLE001 — must capture ANY build failure
        _graph_error = exc
        _graph_traceback = traceback.format_exc()
        # logger.exception here would log correctly too (we are inside an
        # except block), and it preserves the traceback in process logs.
        logger.exception("Chat agent graph initialization failed")
    finally:
        _ready.set()


def get_agent_graph():
    """Sync accessor for callers that cannot await.

    Ensures the chat loop is running, submits graph construction to it,
    and blocks (up to _BUILD_TIMEOUT) until ready. Thread-safe: safe to
    call from any request/worker/test thread.

    Returns the compiled graph, or None if construction failed. On
    failure the ORIGINAL exception is logged with its traceback
    (never a bare generic message).
    """
    if _graph_instance is not None:
        return _graph_instance

    try:
        loop = get_chat_loop()
    except Exception:
        logger.exception("Chat agent graph initialization failed")
        return None

    if not _ready.is_set():
        with _startup_lock:
            if not _ready.is_set():
                thread = threading.Thread(
                    target=_build_in_background, args=(loop,),
                    daemon=True, name="frameiq-chat-graph-init",
                )
                thread.start()
        if not _ready.wait(timeout=_BUILD_TIMEOUT + 5):
            logger.error(
                "Chat agent graph initialization timed out after %ss",
                _BUILD_TIMEOUT + 5,
            )
            return None
        # Give the builder a moment to publish the instance after set().
        deadline = time.time() + 5
        while _graph_instance is None and _graph_error is None \
                and time.time() < deadline:
            time.sleep(0.05)

    if _graph_instance is not None:
        return _graph_instance

    # Real cause, not a generic message (PHASE 9: never hide it).
    if _graph_error is not None:
        logger.error(
            "Chat agent graph is not available: %r\n%s",
            _graph_error, _graph_traceback or "<no traceback captured>",
        )
    else:
        logger.error("Chat agent graph is not available: unknown build failure")
    return None


def submit_to_chat_loop(coro):
    """Submit a coroutine to the chat loop. Thread-safe.

    Returns a concurrent.futures.Future. The coroutine — including any
    graph.astream_events() and AsyncSqliteSaver use — executes ON the
    chat loop, never on the caller's loop.
    """
    loop = get_chat_loop()
    return asyncio.run_coroutine_threadsafe(coro, loop)


def _close_graph():
    """Best-effort cleanup for tests / worker shutdown.

    Closes the saver connection ON the chat loop (properly awaited via
    the submit future) and resets module state so the next
    get_agent_graph() rebuilds. The chat loop thread itself is left
    running — it is process-scoped and cheap to keep.
    """
    global _graph_instance, _graph_error, _graph_traceback, _ready
    prev = _graph_instance
    _graph_instance = None
    _graph_error = None
    _graph_traceback = None
    _ready = threading.Event()
    if prev is not None:
        checkpointer = getattr(prev, "checkpointer", None)
        if checkpointer is not None:
            conn = getattr(checkpointer, "conn", None)
            if conn is not None:
                async def _close_conn():
                    try:
                        await conn.close()
                    except Exception:
                        logger.debug(
                            "Checkpoint connection close failed", exc_info=True,
                        )
                try:
                    fut = submit_to_chat_loop(_close_conn())
                    fut.result(timeout=10)
                except Exception:
                    logger.debug(
                        "Checkpoint connection close timed out", exc_info=True,
                    )
    return prev
