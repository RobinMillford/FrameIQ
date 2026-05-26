"""
Graph node implementations for the FrameIQ LangGraph agent.

- Supervisor: zero-LLM heuristic router (saves 2-3 LLM calls per request).
- Retriever: module-level react agent singleton (not re-created per call).
- Enricher: concurrent TMDb lookups, deduplication.
- retrieved_context: stores actual tool results.
- user_context from state injected into system prompts for personalisation.
"""

import os
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Literal

from dotenv import load_dotenv
from langchain_core.messages import AIMessage, SystemMessage
from langchain_openai import ChatOpenAI
from langgraph.prebuilt import create_react_agent

from .state import GraphState
from .tools import RETRIEVER_TOOLS
from api.chatbot import extract_media_with_llm, is_recent_release, is_upcoming_release

load_dotenv()

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
TMDB_API_KEY = os.getenv("TMDB_API_KEY")

# ── Models ────────────────────────────────────────────────────────────────────
# gpt-4.1-mini : tool calling — reliable structured output for ReAct
# gpt-5-mini   : chat — newer gen, high quality prose
# (supervisor is heuristic — no model needed)

RETRIEVER_MODEL = ChatOpenAI(
    model="gpt-4.1-mini",
    api_key=OPENAI_API_KEY,
    temperature=0,
)

CHAT_MODEL = ChatOpenAI(
    model="gpt-5-mini",
    api_key=OPENAI_API_KEY,
    temperature=0.7,
)

# Module-level singleton — built once, reused across all requests.
_RETRIEVER_AGENT = create_react_agent(RETRIEVER_MODEL, RETRIEVER_TOOLS)

# Shared HTTP session for TMDb calls (connection pooling).
_TMDB_SESSION = requests.Session()


# ── Supervisor (heuristic — zero LLM calls) ───────────────────────────────────

_GREETINGS = {
    "hi", "hello", "hey", "hiya", "howdy", "sup", "yo",
    "thanks", "thank you", "thx", "ty", "cheers",
    "bye", "goodbye", "cya", "ok", "okay", "cool", "great",
}

_CHAT_STARTERS = (
    "what is ", "what are ", "what was ", "explain ", "how does ", "how do ",
    "why is ", "why are ", "tell me about the history", "history of ",
    "difference between", "define ", "meaning of",
)


def supervisor_node(state: GraphState) -> GraphState:
    """
    Zero-LLM heuristic router — saves 2-3 LLM round trips vs the old design.

    Logic:
      greeting / very short pleasantry → "chat" (fast reply, no tools)
      "what is X" / explanation intent → "chat"
      everything else                  → "retriever" (has all 7 tools)
    """
    messages = state["messages"]
    last = messages[-1] if messages else None
    text = (getattr(last, "content", "") or "").strip()
    lower = text.lower()

    # Pure greeting — short pleasantry with no question
    words = lower.split()
    if len(words) <= 4 and set(words) & _GREETINGS:
        return {**state, "next_step": "chat", "user_intent": "chat"}

    # Explanation / definition / history questions → chat (no tools needed)
    if any(lower.startswith(p) for p in _CHAT_STARTERS):
        return {**state, "next_step": "chat", "user_intent": "chat"}

    # Everything else → retriever (recommendations, discovery, filmographies, etc.)
    return {**state, "next_step": "retriever", "user_intent": "search"}


# ── Retriever ─────────────────────────────────────────────────────────────────

def _build_retriever_system(user_context: str) -> str:
    personalisation = (
        f"\n\nUser context (use to personalise recommendations):\n{user_context}"
        if user_context
        else ""
    )
    return (
        "You are a movie/TV research assistant. Choose the RIGHT tool for each query:\n\n"
        "  search_vector_db    — pure vibe/mood/theme queries ('lonely introspective films',\n"
        "                        'movies that feel like a dream', 'dark atmospheric cinema')\n"
        "  search_tmdb         — exact title lookup, release info, cast facts\n"
        "  search_tmdb_person  — filmography of actor/director ('SRK movies', 'Nolan films',\n"
        "                        'movies starring X', 'directed by Y')\n"
        "  discover_movies     — structured movie queries ('best Bollywood 2023', 'top Korean\n"
        "                        thrillers', 'highest rated comedies', 'action films 90s')\n"
        "  discover_tv         — structured TV queries ('best Korean dramas', 'top crime shows',\n"
        "                        'anime 2020s', 'Spanish series')\n"
        "  get_similar_movies  — 'movies like X', 'similar to X', 'more like X'\n"
        "  search_tmdb_trending — 'what's trending', 'what's popular now'\n\n"
        "Rules:\n"
        "- NEVER use search_vector_db for actor/director queries — use search_tmdb_person\n"
        "- NEVER use search_vector_db for language/country queries — use discover_movies/tv\n"
        "- For 'movies like X': use get_similar_movies (TMDb engine), optionally chain with\n"
        "  search_vector_db for vibe refinement\n"
        "- Chain tools when useful\n"
        "- Always label each item as Movie or TV Show\n"
        "- Explain WHY each title fits the request"
        + personalisation
    )


def retriever_node(state: GraphState) -> GraphState:
    """ReAct agent that searches ChromaDB and TMDb then formulates a reply."""
    messages = state["messages"]
    user_context = state.get("user_context") or ""

    system_prompt = _build_retriever_system(user_context)
    result = _RETRIEVER_AGENT.invoke(
        {"messages": [SystemMessage(content=system_prompt), *list(messages)]}
    )

    agent_messages = result["messages"]
    final_message = (
        agent_messages[-1]
        if agent_messages
        else AIMessage(content="I couldn't find any results.")
    )

    # Capture actual tool results (ToolMessage contents), not just args.
    retrieved_context = []
    for msg in agent_messages:
        if hasattr(msg, "type") and msg.type == "tool":
            retrieved_context.append({
                "tool": getattr(msg, "name", "unknown"),
                "result": msg.content[:500] if msg.content else "",
            })

    return {
        **state,
        "messages": list(state["messages"]) + [final_message],
        "retrieved_context": retrieved_context,
    }


# ── Chat ──────────────────────────────────────────────────────────────────────

def _build_chat_system(user_context: str) -> str:
    personalisation = (
        f"\n\nUser context:\n{user_context}" if user_context else ""
    )
    return (
        "You are a knowledgeable and friendly movie/TV expert assistant.\n"
        "Answer questions about films, TV, cinema history, and film concepts.\n"
        "Be conversational. If the user asks for recommendations, provide them "
        "with specific titles and reasons."
        + personalisation
    )


def chat_node(state: GraphState) -> GraphState:
    """Pure LLM chat node — no tools, uses training knowledge."""
    messages = state["messages"]
    user_context = state.get("user_context") or ""

    response = CHAT_MODEL.invoke(
        [SystemMessage(content=_build_chat_system(user_context)), *messages]
    )
    return {
        **state,
        "messages": list(state["messages"]) + [response],
    }


# ── Enricher ──────────────────────────────────────────────────────────────────

def _fetch_tmdb_item(title: str, year, media_type: str) -> dict:
    """Fetch one item from TMDb. Runs in a thread pool."""
    if not TMDB_API_KEY or not title:
        return {}

    url = f"https://api.themoviedb.org/3/search/{media_type}"
    params = {
        "api_key": TMDB_API_KEY,
        "query": title,
        "page": 1,
        "include_adult": False,
    }
    if year:
        params["year" if media_type == "movie" else "first_air_date_year"] = year

    try:
        resp = _TMDB_SESSION.get(url, params=params, timeout=5)
        results = resp.json().get("results", [])

        # Retry without year if empty
        if not results and year:
            params.pop("year", None)
            params.pop("first_air_date_year", None)
            resp = _TMDB_SESSION.get(url, params=params, timeout=5)
            results = resp.json().get("results", [])

        if not results:
            return {
                "title": title,
                "year": str(year) if year else "N/A",
                "poster_url": "https://via.placeholder.com/500x750?text=No+Image",
                "tmdb_link": "#",
                "release_status": " (Not found)",
            }

        info = results[0]
        poster = info.get("poster_path")
        date = (
            info.get("release_date")
            if media_type == "movie"
            else info.get("first_air_date")
        )
        rel_year = date[:4] if date else "Unknown"

        if is_upcoming_release(date):
            status = " (UPCOMING)"
        elif is_recent_release(date):
            status = " (RECENT)"
        else:
            status = ""

        display_title = (
            info.get("title") if media_type == "movie" else info.get("name")
        )
        return {
            "title": display_title,
            "year": rel_year,
            "poster_url": (
                f"https://image.tmdb.org/t/p/w500{poster}"
                if poster
                else "https://via.placeholder.com/500x750?text=No+Image"
            ),
            "tmdb_link": f"/{media_type}/{info.get('id')}",
            "release_status": status,
        }
    except Exception as e:
        print(f"TMDb fetch error for '{title}': {e}")
        return {}


def enricher_node(state: GraphState) -> GraphState:
    """
    Extract titles from the last AI message, fetch TMDb metadata concurrently.
    Deduplicates titles before fetching.
    """
    messages = state["messages"]

    last_ai = next(
        (m.content for m in reversed(messages) if isinstance(m, AIMessage)),
        None,
    )
    if not last_ai:
        return {**state, "final_response_metadata": {"movies": [], "tv_shows": []}}

    try:
        movie_data, tv_show_data = extract_media_with_llm(last_ai)
    except Exception as e:
        print(f"Media extraction error: {e}")
        movie_data, tv_show_data = [], []

    def dedup(items):
        seen, out = set(), []
        for item in items:
            key = (item.get("title", "").lower(), str(item.get("year", "")))
            if key not in seen:
                seen.add(key)
                out.append(item)
        return out

    movie_data = dedup(movie_data)
    tv_show_data = dedup(tv_show_data)

    # Build fetch tasks
    tasks = (
        [(m, "movie") for m in movie_data]
        + [(t, "tv") for t in tv_show_data]
    )

    movies_out, tv_out = [], []
    with ThreadPoolExecutor(max_workers=6) as executor:
        future_map = {
            executor.submit(
                _fetch_tmdb_item,
                item.get("title"),
                item.get("year"),
                mtype,
            ): mtype
            for item, mtype in tasks
        }
        for future in as_completed(future_map):
            mtype = future_map[future]
            result = future.result()
            if result:
                if mtype == "movie":
                    movies_out.append(result)
                else:
                    tv_out.append(result)

    return {
        **state,
        "final_response_metadata": {"movies": movies_out, "tv_shows": tv_out},
    }


# ── Edge function ─────────────────────────────────────────────────────────────

def should_continue(
    state: GraphState,
) -> Literal["retriever", "chat", "__end__"]:
    """Translate supervisor's next_step into a LangGraph edge target."""
    next_step = state.get("next_step", "end")
    if next_step == "end":
        return "__end__"
    return next_step
