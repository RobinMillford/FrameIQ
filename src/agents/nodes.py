"""
Graph node implementations for the FrameIQ LangGraph agent.

- Supervisor: LLM structured-output router (route + entity extraction),
  with a zero-cost heuristic fallback and a fast path for greetings.
- Retriever: module-level react agent singleton (not re-created per call).
- Enricher: concurrent TMDb lookups, deduplication, skipped for short replies.
- retrieved_context: stores actual tool results.
- user_context from state injected into system prompts for personalisation.
"""

import logging
import os
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Literal

from dotenv import load_dotenv
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from langgraph.prebuilt import create_react_agent
from pydantic import BaseModel, Field

from .state import GraphState
from .tools import RETRIEVER_TOOLS, _CURRENT_USER_ID
from api.chatbot import extract_media_with_llm, is_recent_release, is_upcoming_release

logger = logging.getLogger(__name__)

load_dotenv()

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
TMDB_API_KEY = os.getenv("TMDB_API_KEY")

# ── Models ────────────────────────────────────────────────────────────────────
# gpt-4.1-mini : tool calling + supervisor routing — reliable structured output
# gpt-5-mini   : chat — newer gen, high quality prose
# (enricher title extraction uses gpt-5-nano via api/chatbot.py)

RETRIEVER_MODEL = ChatOpenAI(
    model="gpt-4.1-mini",
    api_key=OPENAI_API_KEY,
    temperature=0,
)

# Tags let astream_events distinguish final-response tokens from internal
# model calls (retriever tool decisions, enricher extraction).
CHAT_MODEL = ChatOpenAI(
    model="gpt-5-mini",
    api_key=OPENAI_API_KEY,
    temperature=0.7,
    tags=["final_response"],
)

SUPERVISOR_MODEL = ChatOpenAI(
    model="gpt-4.1-mini",
    api_key=OPENAI_API_KEY,
    temperature=0,
)

# Module-level singleton — built once, reused across all requests.
_RETRIEVER_AGENT = create_react_agent(RETRIEVER_MODEL, RETRIEVER_TOOLS)

# Shared HTTP session for TMDb calls (connection pooling).
_TMDB_SESSION = requests.Session()


# ── Supervisor ────────────────────────────────────────────────────────────────

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


class _RouteDecision(BaseModel):
    """Structured routing decision from the supervisor LLM."""
    route: Literal["greeting", "chat", "retriever"] = Field(
        description=(
            "greeting — pure pleasantry/thanks with no question. "
            "chat — general film knowledge, explanations, definitions; no live "
            "data needed. "
            "retriever — anything needing live data: recommendations, discovery "
            "by genre/year/language/mood, trending, filmographies, similar "
            "titles, or the user's own watch history."
        )
    )
    intent: str = Field(
        description="One-word summary of what the user wants, e.g. "
                    "'recommend', 'trending', 'filmography', 'explain', 'greeting'."
    )
    titles: List[str] = Field(
        default_factory=list,
        description="Movie/TV titles the user mentioned explicitly.",
    )
    people: List[str] = Field(
        default_factory=list,
        description="Actor/director names the user mentioned explicitly.",
    )
    genres: List[str] = Field(
        default_factory=list,
        description="Genres the user asked about, e.g. ['horror', 'sci-fi'].",
    )
    years: List[str] = Field(
        default_factory=list,
        description="Years or decades mentioned, e.g. ['2024', '90s'].",
    )


_SUPERVISOR_CHAIN = None  # lazy singleton


def _get_supervisor_chain():
    global _SUPERVISOR_CHAIN
    if _SUPERVISOR_CHAIN is None:
        _SUPERVISOR_CHAIN = SUPERVISOR_MODEL.with_structured_output(_RouteDecision)
    return _SUPERVISOR_CHAIN


_SUPERVISOR_SYSTEM = (
    "You route messages in a movie/TV recommendation chat app.\n"
    "Rules of thumb:\n"
    "- Pure greetings/thanks with no question → greeting\n"
    "- Needs LIVE data (recommendations, discovery, trending, filmographies,\n"
    "  similar titles, user's own watch history) → retriever\n"
    "- Answerable from general film knowledge (definitions, history,\n"
    "  'what was the first talkie') → chat\n"
    "- 'Suggest me latest horror movies' → retriever (trending/discover)\n"
    "- 'What did I rate Inception' → retriever (user history)\n"
    "- When unsure whether live data helps, choose retriever."
)


def _heuristic_route(text: str) -> tuple:
    """Zero-cost fallback router (previous behaviour). Returns (route, intent)."""
    lower = text.lower().strip()
    words = lower.split()
    if (lower in _GREETINGS) or (len(words) <= 4 and set(words) & _GREETINGS):
        return "greeting", "greeting"
    if any(lower.startswith(p) for p in _CHAT_STARTERS):
        return "chat", "explain"
    return "retriever", "search"


def supervisor_node(state: GraphState) -> GraphState:
    """
    LLM supervisor — structured routing + entity extraction.

    Fast path: obvious greetings skip the LLM call entirely.
    Fallback: heuristic routing if the LLM call fails.
    """
    messages = state["messages"]
    last = messages[-1] if messages else None
    text = (getattr(last, "content", "") or "").strip()

    # Fast path — obvious short greetings, zero latency
    words = text.lower().split()
    if len(words) <= 3 and set(words) & _GREETINGS:
        return {**state, "next_step": "chat", "user_intent": "greeting",
                "entities": {}}

    try:
        decision = _get_supervisor_chain().invoke([
            SystemMessage(content=_SUPERVISOR_SYSTEM),
            HumanMessage(content=text),
        ])
        route = decision.route
        intent = decision.intent or route
        entities = {
            "titles": decision.titles or [],
            "people": decision.people or [],
            "genres": decision.genres or [],
            "years": decision.years or [],
        }
    except Exception as e:
        logger.warning("Supervisor LLM failed, using heuristic: %s", e)
        route, intent = _heuristic_route(text)
        entities = {}

    next_step = "chat" if route in ("greeting", "chat") else "retriever"
    return {**state, "next_step": next_step, "user_intent": intent,
            "entities": entities}


# ── Retriever ─────────────────────────────────────────────────────────────────

_MAX_CONTEXT_MESSAGES = 20


def _trim_messages(messages):
    """Keep the last N messages so checkpoint history doesn't blow the
    context window on long-running threads."""
    msgs = list(messages)
    if len(msgs) <= _MAX_CONTEXT_MESSAGES:
        return msgs
    return msgs[-_MAX_CONTEXT_MESSAGES:]


def _build_retriever_system(user_context: str, entities: dict = None) -> str:
    personalisation = (
        f"\n\nUser context (use to personalise recommendations):\n{user_context}"
        if user_context
        else ""
    )
    entity_hint = ""
    if entities:
        bits = []
        if entities.get("titles"):
            bits.append(f"titles: {entities['titles']}")
        if entities.get("people"):
            bits.append(f"people: {entities['people']}")
        if entities.get("genres"):
            bits.append(f"genres: {entities['genres']}")
        if entities.get("years"):
            bits.append(f"years: {entities['years']}")
        if bits:
            entity_hint = (
                "\n\nEntities already extracted from the user's message "
                "(use these — do not re-parse):\n  " + "; ".join(bits)
            )
    return (
        "You are a movie/TV research assistant. Choose the RIGHT tool for each query:\n\n"
        "  search_tmdb         — exact title lookup, release info, cast facts\n"
        "  search_tmdb_person  — filmography of actor/director ('SRK movies', 'Nolan films',\n"
        "                        'movies starring X', 'directed by Y')\n"
        "  discover_movies     — structured movie queries ('best Bollywood 2023', 'top Korean\n"
        "                        thrillers', 'highest rated comedies', 'action films 90s').\n"
        "                        For VIBE/MOOD queries ('mind-bending heist thriller',\n"
        "                        'dark atmospheric cinema') translate the mood into the\n"
        "                        keywords arg (comma-separated TMDb keywords like\n"
        "                        'heist,twist-ending' or 'time-travel,dystopia') + genre.\n"
        "  discover_tv         — structured TV queries ('best Korean dramas', 'top crime shows',\n"
        "                        'anime 2020s', 'Spanish series'); keywords arg for vibes\n"
        "  get_similar_movies  — 'movies like X', 'similar to X', 'more like X'\n"
        "  search_tmdb_trending — 'what's trending', 'what's popular now', 'latest movies'\n"
        "  my_history          — the user's OWN ratings, diary, watchlist, tracked shows.\n"
        "                        'what did I rate X', 'what's on my watchlist',\n"
        "                        'recommend based on what I watched'\n\n"
        "Rules:\n"
        "- NEVER use discover_* for actor/director queries — use search_tmdb_person\n"
        "- For 'movies like X': use get_similar_movies (TMDb engine)\n"
        "- For personal-history questions ALWAYS use my_history\n"
        "- For mood/vibe questions ALWAYS use discover_* with a keywords arg — never\n"
        "  leave the vibe untranslated\n"
        "- Chain tools when useful\n"
        "- Always label each item as Movie or TV Show\n"
        "- Explain WHY each title fits the request"
        + personalisation
        + entity_hint
    )


def retriever_node(state: GraphState) -> GraphState:
    """ReAct agent that searches TMDb (cached) and user history, then replies."""
    messages = state["messages"]
    user_context = state.get("user_context") or ""
    entities = state.get("entities") or {}

    # Expose the user id to the my_history tool (same thread as agent exec)
    user_id = state.get("user_id")
    _CURRENT_USER_ID.set(user_id)

    system_prompt = _build_retriever_system(user_context, entities)
    result = _RETRIEVER_AGENT.invoke(
        {"messages": [SystemMessage(content=system_prompt),
                      *_trim_messages(messages)]}
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
        "Be conversational. Use markdown for structure (bold titles, bullet "
        "lists) — your reply renders in a markdown-capable chat UI.\n"
        "If the user asks for recommendations, tailor them to the user's "
        "profile below when available, and explain why each pick fits."
        + personalisation
    )


def chat_node(state: GraphState) -> GraphState:
    """Pure LLM chat node — no tools, uses training knowledge."""
    messages = state["messages"]
    user_context = state.get("user_context") or ""

    response = CHAT_MODEL.invoke(
        [SystemMessage(content=_build_chat_system(user_context)),
         *_trim_messages(messages)]
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

    Skips the LLM extraction entirely for short replies (greetings, thanks,
    follow-up chatter) — there are no titles to enrich and each extraction
    costs a model call.
    """
    messages = state["messages"]

    last_ai = next(
        (m for m in reversed(messages) if isinstance(m, AIMessage)),
        None,
    )
    last_text = (getattr(last_ai, "content", "") or "") if last_ai else ""
    last_text = last_text if isinstance(last_text, str) else "".join(
        str(p) for p in last_text)

    # Skip: nothing to enrich for short conversational replies
    if len(last_text.strip()) < 120:
        return {**state, "final_response_metadata": {"movies": [], "tv_shows": []}}

    try:
        movie_data, tv_show_data = extract_media_with_llm(last_text)
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
