"""
Main StateGraph construction for the LangGraph multi-agent system.

This module builds the complete workflow with conditional routing.
"""

from langgraph.graph import StateGraph, START, END
from langgraph.checkpoint.memory import MemorySaver

from .state import GraphState
from .nodes import (
    supervisor_node,
    retriever_node,
    chat_node,
    enricher_node,
    should_continue
)


def create_agent_graph():
    """
    Create and compile the LangGraph StateGraph for FrameIQ.
    
    Workflow:
        START → supervisor → [retriever | chat | enricher | END]
        retriever → supervisor (re-evaluate)
        chat → supervisor (re-evaluate)
        enricher → END
    
    Returns:
        Compiled StateGraph with memory checkpointing
    """
    # Initialize the graph
    workflow = StateGraph(GraphState)
    
    # Add nodes
    workflow.add_node("supervisor", supervisor_node)
    workflow.add_node("retriever", retriever_node)
    workflow.add_node("chat", chat_node)
    workflow.add_node("enricher", enricher_node)
    
    # Define edges
    # supervisor → retriever or chat (heuristic, zero LLM calls)
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
    
    # Compile with memory checkpointing
    memory = MemorySaver()
    graph = workflow.compile(checkpointer=memory)
    
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
