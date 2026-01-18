"""
Graph node implementations for the LangGraph multi-agent system.

Each node is a function that takes GraphState and returns updated state.
"""

from typing import Literal
from langchain_core.messages import HumanMessage, AIMessage, SystemMessage
from langchain_groq import ChatGroq
from langgraph.prebuilt import create_react_agent
import os
import json
import re
import requests
from dotenv import load_dotenv

from .state import GraphState, SupervisorDecision
from .tools import RETRIEVER_TOOLS
from api.chatbot import extract_media_with_llm, is_recent_release, is_upcoming_release

load_dotenv()

GROQ_API_KEY = os.getenv("groq_api_key")
TMDB_API_KEY = os.getenv("TMDB_API_KEY")

# Initialize models with optimal selection for each task
# Supervisor: Fast routing decisions (Llama 3.1 8B Instant)
SUPERVISOR_MODEL = ChatGroq(
    model="llama-3.1-8b-instant",
    api_key=GROQ_API_KEY,
    temperature=0
)

# Retriever: Fast tool execution and search (Llama 3.1 8B Instant)
RETRIEVER_MODEL = ChatGroq(
    model="llama-3.1-8b-instant",
    api_key=GROQ_API_KEY,
    temperature=0
)

# Chat: Deep analysis and nuanced recommendations (Llama 3.3 70B Versatile)
CHAT_MODEL = ChatGroq(
    model="llama-3.3-70b-versatile",
    api_key=GROQ_API_KEY,
    temperature=0.7
)

# Enrichment: Uses Llama 3.3 70B for title extraction (via extract_media_with_llm)


def supervisor_node(state: GraphState) -> GraphState:
    """
    Supervisor node: Analyzes user intent and routes to appropriate agent.
    
    Routes:
        - "retriever" → User wants movie/TV recommendations or specific info
        - "chat" → General questions, greetings, explanations
        - "enricher" → Retrieval complete, needs UI metadata enrichment
        - "end" → Conversation complete
    """
    messages = state["messages"]
    
    # Check if we just got a response from an agent
    if len(messages) >= 2:
        last_message = messages[-1]
        second_last_message = messages[-2] if len(messages) >= 2 else None
        
        # If last message is from AI
        if isinstance(last_message, AIMessage):
            content_lower = last_message.content.lower()
            
            # Check if it contains movie recommendations (needs enrichment)
            recommendation_keywords = ["recommend", "suggest", "check out", "might enjoy", "here are", "similar to"]
            has_recommendations = any(keyword in content_lower for keyword in recommendation_keywords)
            
            # Check if it's a simple explanation (should end)
            is_explanation = any(keyword in content_lower for keyword in ["film noir", "genre", "style", "technique", "director", "cinematography"])
            is_greeting_response = any(keyword in content_lower for keyword in ["i'd be happy", "i'm here to help", "what can i help"])
            
            # If it has movie recommendations, enrich them
            if has_recommendations and not is_greeting_response:
                return {
                    **state,
                    "next_step": "enricher",
                    "user_intent": "enrich"
                }
            
            # If it's just an explanation or greeting, end the conversation
            if (is_explanation or is_greeting_response) and not has_recommendations:
                return {
                    **state,
                    "next_step": "end",
                    "user_intent": "end"
                }
    
    # For new user messages, route based on intent
    if len(messages) >= 1 and isinstance(messages[-1], HumanMessage):
        user_message = messages[-1].content.lower()
        
        # Check for recommendation requests
        if any(keyword in user_message for keyword in ["suggest", "recommend", "like", "similar", "trending", "recent", "new", "what should i watch"]):
            return {
                **state,
                "next_step": "retriever",
                "user_intent": "search"
            }
        
        # Check for general questions
        if any(keyword in user_message for keyword in ["what is", "who", "when", "where", "how", "explain", "tell me about"]):
            return {
                **state,
                "next_step": "chat",
                "user_intent": "chat"
            }
        
        # Check for greetings
        if any(keyword in user_message for keyword in ["hello", "hi", "hey", "thanks", "thank you", "bye", "goodbye"]):
            return {
                **state,
                "next_step": "chat",
                "user_intent": "chat"
            }
    
    # Default: use LLM to decide
    system_prompt = """You are a routing supervisor. Analyze the conversation and decide:
- "retriever" if user wants movie/TV recommendations
- "chat" if user asks general questions or greetings
- "end" if conversation is complete

Return your decision."""
    
    structured_llm = SUPERVISOR_MODEL.with_structured_output(SupervisorDecision)
    decision = structured_llm.invoke([
        SystemMessage(content=system_prompt),
        *messages[-3:]  # Only last 3 messages to avoid context overflow
    ])
    
    next_step = decision["next_step"]
    intent_map = {
        "retriever": "search",
        "chat": "chat",
        "enricher": "enrich",
        "end": "end"
    }
    
    return {
        **state,
        "next_step": next_step,
        "user_intent": intent_map.get(next_step, "end")
    }


def retriever_node(state: GraphState) -> GraphState:
    """
    Retriever agent: Uses tools to search ChromaDB and TMDb API.
    
    The agent decides which tool(s) to use based on the query.
    """
    messages = state["messages"]
    
    # System prompt for retriever
    system_prompt = """You are a movie/TV show research assistant with access to:
1. A vector database of movies/shows (search_vector_db) - for semantic similarity
2. TMDb API (search_tmdb) - for specific titles and recent releases
3. Trending data (search_tmdb_trending) - for what's popular now

**Guidelines:**
- Use search_vector_db for: "movies like X", vibes, themes, genres
- Use search_tmdb for: specific titles, recent releases (2022-2025), factual info
- Use search_tmdb_trending for: "what's trending", "what's popular"
- You can use MULTIPLE tools if needed
- Provide detailed, conversational recommendations
- Always mention if something is a Movie or TV Show
- Explain WHY you're recommending each item (similar themes, genres, style)

Be helpful and thorough!"""

    # Create react agent with tools
    agent = create_react_agent(RETRIEVER_MODEL, RETRIEVER_TOOLS)
    
    # Run the agent
    result = agent.invoke({
        "messages": [SystemMessage(content=system_prompt)] + list(messages)
    })
    
    # Extract the final AI message
    agent_messages = result["messages"]
    final_message = agent_messages[-1] if agent_messages else AIMessage(content="I couldn't find any results.")
    
    # Store retrieved context (from tool calls)
    retrieved_context = []
    for msg in agent_messages:
        if hasattr(msg, 'tool_calls') and msg.tool_calls:
            for tool_call in msg.tool_calls:
                retrieved_context.append({
                    "tool": tool_call.get("name"),
                    "args": tool_call.get("args")
                })
    
    return {
        **state,
        "messages": state["messages"] + [final_message],
        "retrieved_context": retrieved_context
    }


def chat_node(state: GraphState) -> GraphState:
    """
    Chat agent: Handles general questions without tools.
    
    Uses pure LLM knowledge for explanations, greetings, etc.
    """
    messages = state["messages"]
    
    system_prompt = """You are a friendly movie/TV expert assistant.

Answer general questions about movies, TV shows, cinema history, and film concepts.
Be conversational and helpful. If the user asks for recommendations, suggest they ask more specifically."""

    response = CHAT_MODEL.invoke([
        SystemMessage(content=system_prompt),
        *messages
    ])
    
    return {
        **state,
        "messages": state["messages"] + [response]
    }


def enricher_node(state: GraphState) -> GraphState:
    """
    Enricher node: Extracts movie titles from AI response and fetches TMDb metadata.
    
    This is a deterministic post-processing step that adds visual cards to the UI.
    """
    messages = state["messages"]
    
    # Get the last AI message
    last_ai_message = None
    for msg in reversed(messages):
        if isinstance(msg, AIMessage):
            last_ai_message = msg.content
            break
    
    if not last_ai_message:
        return {
            **state,
            "final_response_metadata": {"movies": [], "tv_shows": []}
        }
    
    # Extract movie/TV titles using LLM
    try:
        movie_data, tv_show_data = extract_media_with_llm(last_ai_message)
    except Exception as e:
        print(f"Error extracting media: {e}")
        movie_data, tv_show_data = [], []
    
    # Fetch metadata from TMDb
    media_data = {"movies": [], "tv_shows": []}
    
    for media_list, media_type, key in [(movie_data, "movie", "movies"), (tv_show_data, "tv", "tv_shows")]:
        for media in media_list:
            title = media.get("title")
            year = media.get("year")
            
            if not title:
                continue
            
            # Search TMDb
            url = f"https://api.themoviedb.org/3/search/{media_type}"
            params = {
                "api_key": TMDB_API_KEY,
                "query": title,
                "page": 1,
                "include_adult": True
            }
            if year:
                params["year" if media_type == "movie" else "first_air_date_year"] = year
            
            try:
                response = requests.get(url, params=params, timeout=5)
                results = response.json().get("results", [])
                
                # Retry without year if no results
                if not results and year:
                    params.pop("year", None)
                    params.pop("first_air_date_year", None)
                    response = requests.get(url, params=params, timeout=5)
                    results = response.json().get("results", [])
                
                if results:
                    media_info = results[0]
                    poster_path = media_info.get("poster_path")
                    release_date = media_info.get("release_date") if media_type == "movie" else media_info.get("first_air_date")
                    release_year = release_date[:4] if release_date else "Unknown"
                    
                    # Determine status
                    is_recent = is_recent_release(release_date)
                    is_upcoming = is_upcoming_release(release_date)
                    release_status = ""
                    if is_upcoming:
                        release_status = " (UPCOMING)"
                    elif is_recent:
                        release_status = " (RECENT)"
                    
                    media_data[key].append({
                        "title": media_info.get("title") if media_type == "movie" else media_info.get("name"),
                        "year": release_year,
                        "poster_url": f"https://image.tmdb.org/t/p/w500{poster_path}" if poster_path else "https://via.placeholder.com/500x750?text=No+Image",
                        "tmdb_link": f"/{media_type}/{media_info.get('id')}",
                        "release_status": release_status
                    })
                else:
                    # Placeholder for not found
                    media_data[key].append({
                        "title": title,
                        "year": year if year else "N/A",
                        "poster_url": "https://via.placeholder.com/500x750?text=No+Image",
                        "tmdb_link": "#",
                        "release_status": " (Not found in database)"
                    })
            
            except Exception as e:
                print(f"Error fetching TMDb data for {title}: {e}")
                continue
    
    return {
        **state,
        "final_response_metadata": media_data
    }


def should_continue(state: GraphState) -> Literal["retriever", "chat", "enricher", "__end__"]:
    """
    Conditional edge function for routing from supervisor.
    """
    next_step = state.get("next_step", "end")
    
    if next_step == "end":
        return "__end__"
    
    return next_step
