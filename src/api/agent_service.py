"""
Enhanced API service layer with production features.

Includes error handling, monitoring, conversation memory, and streaming support.
"""

from typing import Dict, Any, Generator
from langchain_core.messages import HumanMessage, AIMessage
from src.agents.graph import get_agent_graph
from src.agents.state import GraphState
from src.agents.error_handling import retry_on_error, get_fallback_response
from src.agents.memory import (
    update_conversation_metadata,
    get_conversation_context,
    save_conversation_to_disk
)
from src.agents.monitoring import (
    track_performance,
    log_agent_decision,
    get_performance_metrics
)
import logging

logger = logging.getLogger(__name__)


@track_performance
@retry_on_error(max_retries=2, delay=1.0)
def run_agent_chat(user_message: str, session_id: str) -> Dict[str, Any]:
    """
    Run the LangGraph agent workflow for a user message.
    
    Enhanced with error handling, monitoring, and conversation memory.
    
    Args:
        user_message: User's chat message
        session_id: Session identifier for state persistence
    
    Returns:
        Dictionary with:
            - reply: AI response text
            - movies: List of movie metadata (optional)
            - tv_shows: List of TV show metadata (optional)
            - metadata: Performance and conversation metadata
    """
    # Get the compiled graph
    graph = get_agent_graph()
    
    # Get conversation context
    context = get_conversation_context(session_id)
    logger.info(f"Session context: {context}")
    
    # Create initial state
    initial_state: GraphState = {
        "messages": [HumanMessage(content=user_message)],
        "user_intent": None,
        "next_step": None,
        "retrieved_context": [],
        "final_response_metadata": {"movies": [], "tv_shows": []}
    }
    
    # Configure with session-based checkpointing and recursion limit
    config = {
        "configurable": {
            "thread_id": session_id
        },
        "recursion_limit": 15  # Prevent infinite loops while allowing multi-turn
    }
    
    try:
        # Run the graph
        final_state = graph.invoke(initial_state, config)
        
        # Extract the final AI message
        messages = final_state["messages"]
        final_reply = ""
        for msg in reversed(messages):
            if hasattr(msg, 'content') and msg.content:
                final_reply = msg.content
                break
        
        # Update conversation memory
        update_conversation_metadata(
            session_id,
            message_count=len(messages),
            metadata={
                "last_intent": final_state.get("user_intent"),
                "last_route": final_state.get("next_step")
            }
        )
        
        # Log the routing decision
        log_agent_decision(
            node_name="supervisor",
            decision=final_state.get("next_step", "unknown"),
            reasoning=f"Intent: {final_state.get('user_intent')}",
            metadata={"session_id": session_id}
        )
        
        # Save conversation to disk (optional persistence)
        save_conversation_to_disk(session_id, final_state)
        
        # Build response
        response = {
            "reply": final_reply,
            "metadata": {
                "session_id": session_id,
                "message_count": len(messages),
                "route": final_state.get("next_step"),
                "intent": final_state.get("user_intent")
            }
        }
        
        # Add enriched metadata if available
        metadata = final_state.get("final_response_metadata", {})
        if metadata.get("movies"):
            response["movies"] = metadata["movies"]
        if metadata.get("tv_shows"):
            response["tv_shows"] = metadata["tv_shows"]
        
        return response
    
    except Exception as e:
        logger.error(f"Error in agent workflow: {e}", exc_info=True)
        
        # Determine error type
        error_type = "llm_error"
        if "rate" in str(e).lower() or "429" in str(e):
            error_type = "rate_limit"
        elif "timeout" in str(e).lower():
            error_type = "timeout"
        
        # Return fallback response
        return {
            "reply": get_fallback_response(error_type, user_message),
            "error": str(e),
            "metadata": {
                "session_id": session_id,
                "error_type": error_type
            }
        }


def run_agent_chat_streaming(
    user_message: str,
    session_id: str
) -> Generator[Dict[str, Any], None, None]:
    """
    Run the agent workflow with streaming support.
    
    Yields state updates as they occur for real-time UI updates.
    
    Args:
        user_message: User's chat message
        session_id: Session identifier
    
    Yields:
        State updates with node names and partial results
    """
    graph = get_agent_graph()
    
    initial_state: GraphState = {
        "messages": [HumanMessage(content=user_message)],
        "user_intent": None,
        "next_step": None,
        "retrieved_context": [],
        "final_response_metadata": {"movies": [], "tv_shows": []}
    }
    
    config = {
        "configurable": {
            "thread_id": session_id
        },
        "recursion_limit": 15
    }
    
    try:
        # Stream state updates
        for state_update in graph.stream(initial_state, config):
            # Extract node name and state
            for node_name, node_state in state_update.items():
                # Get the latest message if available
                messages = node_state.get("messages", [])
                latest_message = ""
                
                if messages:
                    last_msg = messages[-1]
                    if hasattr(last_msg, 'content'):
                        latest_message = last_msg.content
                
                # Yield update
                yield {
                    "node": node_name,
                    "message": latest_message,
                    "intent": node_state.get("user_intent"),
                    "next_step": node_state.get("next_step"),
                    "metadata": node_state.get("final_response_metadata", {})
                }
        
        # Update conversation memory after streaming completes
        update_conversation_metadata(session_id, message_count=len(messages))
    
    except Exception as e:
        logger.error(f"Streaming error: {e}", exc_info=True)
        yield {
            "node": "error",
            "message": get_fallback_response("llm_error", user_message),
            "error": str(e)
        }


def get_agent_metrics() -> Dict[str, Any]:
    """
    Get performance metrics for the agent system.
    
    Returns:
        Dictionary with performance statistics
    """
    return get_performance_metrics()
