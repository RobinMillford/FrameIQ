"""
Main StateGraph construction for the FrameIQ multi-agent system.

This module builds the complete workflow with conditional routing
and persistent (SQLite) conversation checkpointing.
"""

import asyncio
import os

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

_graph_instance = None


async def _build_async_ckpt_saver():
    """Build an AsyncSqliteSaver bound to a dedicated aiosqlite connection.

    AsyncSqliteSaver in langgraph-checkpoint-sqlite >= 2 expects to own
    the event loop reference at construction time, so it must be built
    inside a running event loop. We create one short-lived loop here,
    build the saver, then let the loop close — the saver holds its own
    internal loop reference and connection handle.

    The SQLite connection is configured with WAL mode and a busy timeout
    so concurrent Gunicorn workers can checkpoint the same file safely.
    """
    import aiosqlite

    async def _make_saver():
        db_conn = await aiosqlite.connect(_CHECKPOINT_DB)
        # Enable WAL + busy timeout once at startup so the checkpoint DB
        # tolerates concurrent writer checkout from multiple workers.
        await db_conn.execute("PRAGMA journal_mode=WAL")
        await db_conn.execute("PRAGMA busy_timeout=5000")
        await db_conn.commit()
        saver = AsyncSqliteSaver(db_conn)
        await saver.setup()
        return saver

    try:
        return await asyncio.wait_for(_make_saver(), timeout=10)
    except Exception:
        # If checkpoint setup fails (missing aiosqlite, disk full, bad path,
        # locked DB, etc.), fall back to an in-memory checkpointer so chat
        # still streams rather than hard-failing. Persistence is lost only in
        # this degraded case and is logged clearly.
        import logging
        from langgraph.checkpoint.memory import MemorySaver
        logger = logging.getLogger(__name__)
        logger.exception(
            "AsyncSqliteSaver setup failed; using in-memory checkpoint fallback"
        )
        return MemorySaver()


async def _get_agent_graph_async():
    """Async singleton builder — graph is compiled once per process lifetime."""
    global _graph_instance
    if _graph_instance is None:
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

        saver = await _build_async_ckpt_saver()
        _graph_instance = workflow.compile(checkpointer=saver)
    return _graph_instance


def get_agent_graph():
    """Sync accessor kept for any sync callers; builds graph on first use."""
    if _graph_instance is not None:
        return _graph_instance

    try:
        loop = asyncio.new_event_loop()
    except Exception:
        return None

    try:
        graph = loop.run_until_complete(_get_agent_graph_async())
    except Exception:
        try:
            loop.run_until_complete(loop.shutdown_asyncgens())
        except Exception:
            pass
        try:
            loop.close()
        except Exception:
            pass
        return None
    finally:
        try:
            loop.run_until_complete(loop.shutdown_asyncgens())
        except Exception:
            pass
        try:
            loop.close()
        except Exception:
            pass

    return graph
