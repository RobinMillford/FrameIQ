"""
State definition for the LangGraph multi-agent system.

This module defines the shared state that flows through all nodes in the graph.
"""

from typing import TypedDict, Annotated, Sequence, Optional, List, Dict, Any
from langchain_core.messages import BaseMessage
from langgraph.graph.message import add_messages


class GraphState(TypedDict):
    """
    State schema for the FrameIQ agent graph.
    
    Attributes:
        messages: Conversation history (automatically merged via add_messages)
        user_intent: Routing decision ("search", "chat", "enrich", "end")
        retrieved_context: Documents/movies found by retriever agent
        final_response_metadata: UI payload with posters, ratings, TMDb links
        next_step: Supervisor's routing decision for conditional edges
    """
    
    # Conversation history - uses add_messages reducer to append/merge
    messages: Annotated[Sequence[BaseMessage], add_messages]
    
    # Routing and intent
    user_intent: Optional[str]
    next_step: Optional[str]
    
    # Retrieved data from tools
    retrieved_context: List[Dict[str, Any]]
    
    # Final enriched metadata for UI
    final_response_metadata: Dict[str, Any]


class SupervisorDecision(TypedDict):
    """
    Structured output from the supervisor node.
    
    Used for routing decisions via function calling.
    """
    next_step: str  # One of: "retriever", "chat", "enricher", "end"
    reasoning: str  # Why this route was chosen
