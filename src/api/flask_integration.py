"""
Enhanced Flask route integration with streaming and metrics endpoints.
"""
import logging
from flask import Blueprint, request, jsonify, Response, stream_with_context
from flask_login import login_required, current_user
from extensions import limiter
from src.api.agent_service import (
    run_agent_chat,
    run_agent_chat_streaming,
    get_agent_metrics
)
import json

logger = logging.getLogger(__name__)

agent_chat = Blueprint('agent_chat', __name__)


@agent_chat.route("/agent_chat_api", methods=["POST"])
@login_required
@limiter.limit("20 per minute; 100 per hour")
def agent_chat_api():
    user_message = request.json.get("message")
    if not user_message:
        return jsonify({"error": "Message is required"}), 400

    # Per-user session — prevents cross-user context leakage under NAT
    session_id = f"user_{current_user.id}"

    response = run_agent_chat(user_message, session_id)
    return jsonify(response)


@agent_chat.route("/agent_chat_stream", methods=["POST"])
@login_required
@limiter.limit("20 per minute; 100 per hour")
def agent_chat_stream():
    user_message = request.json.get("message")
    if not user_message:
        return jsonify({"error": "Message is required"}), 400

    session_id = f"user_{current_user.id}"

    def generate():
        for update in run_agent_chat_streaming(user_message, session_id):
            data = json.dumps(update)
            yield f"data: {data}\n\n"
        yield 'data: {"done": true}\n\n'

    return Response(
        stream_with_context(generate()),
        mimetype='text/event-stream',
        headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'}
    )


@agent_chat.route("/agent_metrics", methods=["GET"])
@login_required
def agent_metrics():
    metrics = get_agent_metrics()
    return jsonify(metrics)


@agent_chat.route("/agent_health", methods=["GET"])
@login_required
def agent_health():
    try:
        from src.agents.graph import get_agent_graph
        graph = get_agent_graph()
        return jsonify({"status": "healthy", "graph_loaded": graph is not None})
    except Exception as e:
        logger.error("Agent health check failed: %s", e)
        return jsonify({"status": "unhealthy", "error": str(e)}), 500
