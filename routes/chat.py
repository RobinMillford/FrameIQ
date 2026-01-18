from flask import Blueprint, render_template, request, jsonify, Response, stream_with_context
import json
import time
from flask_login import login_required, current_user
import requests
import os
from api.chatbot import get_chatbot, clean_json_response, is_recent_release, is_upcoming_release, is_safety_model_response, extract_media_with_llm
from api.rag_helper import enhance_prompt_with_rag
from langchain.schema import AIMessage, HumanMessage
import json
from datetime import datetime
import re

# Get environment variables
TMDB_API_KEY = os.getenv("TMDB_API_KEY")

chat = Blueprint('chat', __name__)

# In-memory session storage
chat_sessions = {}

@chat.route("/chat")
@login_required
def chat_page():
    """Chat page - now uses optimized agent system with automatic model selection"""
    session_id = request.remote_addr
    if session_id not in chat_sessions:
        chat_sessions[session_id] = []
    return render_template("chat.html")

@chat.route("/chat_api", methods=["POST"])
@login_required
def chat_api():
    """Streaming chat API with real-time progress updates using LangGraph agents."""
    user_message = request.json.get("message")
    if not user_message:
        return jsonify({"error": "Message is required"}), 400
    
    session_id = request.remote_addr
    if session_id not in chat_sessions:
        chat_sessions[session_id] = []
    
    def generate():
        """Generate streaming progress updates."""
        from src.agents.graph import get_agent_graph
        from src.agents.state import GraphState
        from langchain_core.messages import HumanMessage, AIMessage
        
        try:
            # Yield initial status
            yield f"data: {json.dumps({'status': 'Analyzing your query...', 'type': 'progress'})}\n\n"
            
            # Get the compiled graph
            graph = get_agent_graph()
            
            # Create initial state
            initial_state: GraphState = {
                "messages": [HumanMessage(content=user_message)],
                "user_intent": None,
                "next_step": None,
                "retrieved_context": [],
                "final_response_metadata": {"movies": [], "tv_shows": []}
            }
            
            config = {
                "configurable": {"thread_id": session_id},
                "recursion_limit": 15
            }
            
            # Stream the graph execution
            final_state = None
            for state_update in graph.stream(initial_state, config):
                for node_name, node_state in state_update.items():
                    # Send progress based on node
                    if node_name == "supervisor":
                        intent = node_state.get("user_intent", "")
                        if intent == "search":
                            yield f"data: {json.dumps({'status': '🔍 Searching for movies...', 'type': 'progress'})}\n\n"
                        elif intent == "chat":
                            yield f"data: {json.dumps({'status': '💭 Thinking...', 'type': 'progress'})}\n\n"
                    
                    elif node_name == "retriever":
                        yield f"data: {json.dumps({'status': '📊 Searching vector database (8,945 movies)...', 'type': 'progress'})}\n\n"
                        time.sleep(0.5)  # Brief pause for UX
                        yield f"data: {json.dumps({'status': '🎬 Querying TMDb API...', 'type': 'progress'})}\n\n"
                    
                    elif node_name == "chat":
                        yield f"data: {json.dumps({'status': '🤖 Generating response...', 'type': 'progress'})}\n\n"
                    
                    elif node_name == "enricher":
                        yield f"data: {json.dumps({'status': '🎨 Fetching movie posters...', 'type': 'progress'})}\n\n"
                    
                    final_state = node_state
            
            # Extract final response
            if final_state:
                messages = final_state["messages"]
                final_reply = ""
                for msg in reversed(messages):
                    if hasattr(msg, 'content') and msg.content:
                        final_reply = msg.content
                        break
                
                # Build response
                response = {
                    "reply": final_reply,
                    "type": "final"
                }
                
                # Add enriched metadata
                metadata = final_state.get("final_response_metadata", {})
                if metadata.get("movies"):
                    response["movies"] = metadata["movies"]
                if metadata.get("tv_shows"):
                    response["tv_shows"] = metadata["tv_shows"]
                
                # Update session history
                chat_sessions[session_id].append({"type": "human", "content": user_message})
                chat_sessions[session_id].append({"type": "ai", "content": final_reply})
                
                yield f"data: {json.dumps(response)}\n\n"
            else:
                yield f"data: {json.dumps({'error': 'No response generated', 'type': 'error'})}\n\n"
        
        except Exception as e:
            print(f"Error in streaming chat: {e}")
            import traceback
            traceback.print_exc()
            yield f"data: {json.dumps({'error': str(e), 'type': 'error'})}\n\n"
    
    return Response(
        stream_with_context(generate()),
        mimetype='text/event-stream',
        headers={
            'Cache-Control': 'no-cache',
            'X-Accel-Buffering': 'no'
        }
    )
