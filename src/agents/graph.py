"""
Main StateGraph construction for the FrameIQ multi-agent system.

This module builds the complete workflow with conditional routing
and persistent (SQLite) conversation checkpointing.
"""

import os
import sqlite3

from langgraph.graph import StateGraph, START, END
from langgraph.checkpoint.sqlite import SqliteSaver

from .state import GraphState
from .nodes import (
    supervisor_node,
    retriever_node,
    chat_node,
    enricher_node,
    should_continue
)

# Conversation memory survives server restarts. Location is overridable
# for tests (in-memory SQLite via ":memory:" would defeat persistence,
# so a file path is the default).
_CHECKPOINT_DB = os.getenv(
    "CHAT_CHECKPOINT_DB",
    os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__)))), "instance", "chat_memory.db"
    ),
)
os.makedirs(os.path.dirname(_CHECKPOINT_DB), exist_ok=True)

_checkpoint_connection = None


def create_agent_graph():
    """
    Create and compile the LangGraph StateGraph for FrameIQ.

    Workflow:
        START → supervisor → [retriever | chat] → enricher → END

    Returns:
        Compiled StateGraph with SQLite checkpointing
    """
    # Initialize the graph
    workflow = StateGraph(GraphState)

    # Add nodes
    workflow.add_node("supervisor", supervisor_node)
    workflow.add_node("retriever", retriever_node)
    workflow.add_node("chat", chat_node)
    workflow.add_node("enricher", enricher_node)

    # Define edges
    # supervisor → retriever or chat (LLM structured routing)
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

    # retriever / chat → enricher (no loop-back to supervisor)
    workflow.add_edge("retriever", "enricher")
    workflow.add_edge("chat", "enricher")
    workflow.add_edge("enricher", END)

    # Keep one SQLite connection open for the process so checkpoints survive
    # application restarts through the mounted instance volume.
    global _checkpoint_connection
    if _checkpoint_connection is None:
        _checkpoint_connection = sqlite3.connect(
            _CHECKPOINT_DB, check_same_thread=False,
        )
    saver = SqliteSaver(_checkpoint_connection)
    saver.setup()
    graph = workflow.compile(checkpointer=saver)

    return graph


# Create singleton instance
_graph_instance = None


def get_agent_graph():
    """
    Get or create the singleton graph instance.

    Returns:
        Compiled StateGraph
    """
    global _graph_instance
    if _graph_instance is None:
        _graph_instance = create_agent_graph()
    return _graph_instance
