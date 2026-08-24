"""
Flask integration for the agent system — metrics and health endpoints.

Chat traffic flows through routes/chat.py (/chat_api). The former
duplicate endpoints (/agent_chat_api, /agent_chat_stream) were removed
— they were unused by any frontend code.
"""
import logging
from flask import Blueprint, jsonify
from flask_login import login_required
from src.api.agent_service import get_agent_metrics

logger = logging.getLogger(__name__)

agent_chat = Blueprint('agent_chat', __name__)


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
