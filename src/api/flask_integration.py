"""
Enhanced Flask route integration with streaming and metrics endpoints.
"""

from flask import Blueprint, request, jsonify, Response, stream_with_context
from flask_login import login_required
from src.api.agent_service import (
    run_agent_chat,
    run_agent_chat_streaming,
    get_agent_metrics
)
import json

# Create blueprint for agent-based chat
agent_chat = Blueprint('agent_chat', __name__)


@agent_chat.route("/agent_chat_api", methods=["POST"])
@login_required
def agent_chat_api():
    """
    Enhanced agent-based chat endpoint with full error handling.
    """
    user_message = request.json.get("message")
    
    if not user_message:
        return jsonify({"error": "Message is required"}), 400
    
    # Use IP address as session ID (same as existing implementation)
    session_id = request.remote_addr
    
    # Run the agent workflow
    response = run_agent_chat(user_message, session_id)
    
    # Return in the same format as existing endpoint
    return jsonify(response)


@agent_chat.route("/agent_chat_stream", methods=["POST"])
@login_required
def agent_chat_stream():
    """
    Streaming endpoint for real-time agent responses.
    
    Returns Server-Sent Events (SSE) for live updates.
    """
    user_message = request.json.get("message")
    
    if not user_message:
        return jsonify({"error": "Message is required"}), 400
    
    session_id = request.remote_addr
    
    def generate():
        """Generate SSE events."""
        for update in run_agent_chat_streaming(user_message, session_id):
            # Format as SSE
            data = json.dumps(update)
            yield f"data: {data}\n\n"
        
        # Send completion event
        yield "data: {\"done\": true}\n\n"
    
    return Response(
        stream_with_context(generate()),
        mimetype='text/event-stream',
        headers={
            'Cache-Control': 'no-cache',
            'X-Accel-Buffering': 'no'
        }
    )


@agent_chat.route("/agent_metrics", methods=["GET"])
@login_required
def agent_metrics():
    """
    Get agent performance metrics.
    
    Useful for monitoring and debugging.
    """
    metrics = get_agent_metrics()
    return jsonify(metrics)


@agent_chat.route("/agent_health", methods=["GET"])
def agent_health():
    """
    Health check endpoint for the agent system.
    """
    try:
        from src.agents.graph import get_agent_graph
        graph = get_agent_graph()
        
        return jsonify({
            "status": "healthy",
            "graph_loaded": graph is not None,
            "timestamp": json.dumps({"now": True})
        })
    except Exception as e:
        return jsonify({
            "status": "unhealthy",
            "error": str(e)
        }), 500
