"""
RAG Helper — vector DB search + TMDb fallback for the agent tools.

Only the functions actively called by the LangGraph agent live here.
"""

from api.vector_db import get_vector_db
from datetime import datetime
from typing import Dict, Any, Optional


try:
    vector_db = get_vector_db()
    print(f"RAG: Vector DB loaded ({vector_db.count_movies()} items)")
    RAG_ENABLED = True
except Exception as e:
    print(f"RAG: Vector DB unavailable: {e}")
    vector_db = None
    RAG_ENABLED = False


def search_vector_db(
    query: str, top_k: int = 5
) -> Optional[Dict[str, Any]]:
    """Semantic search against ChromaDB. Returns raw ChromaDB result dict."""
    if not RAG_ENABLED or vector_db is None:
        return None
    try:
        return vector_db.search(query, top_k=top_k)
    except Exception as e:
        print(f"Vector DB search error: {e}")
        return None


def search_tmdb_for_media(
    title: str, year: Optional[str] = None
) -> Optional[Dict[str, Any]]:
    """
    Search TMDb for a specific title. Tries TV first, then movies.
    Returns a dict with title/year/overview/media_type/id or None.
    """
    try:
        import os

        api_key = os.getenv("TMDB_API_KEY")
        if not api_key:
            return None

        session = _get_tmdb_session()
        params_base = {
            "api_key": api_key, "query": title, "include_adult": False
        }

        # TV first
        params = dict(params_base)
        if year:
            params["first_air_date_year"] = year
        resp = session.get(
            "https://api.themoviedb.org/3/search/tv",
            params=params, timeout=5
        )
        results = resp.json().get("results", [])
        media_type = "tv"

        if not results:
            params = dict(params_base)
            if year:
                params["year"] = year
            resp = session.get(
                "https://api.themoviedb.org/3/search/movie",
                params=params, timeout=5
            )
            results = resp.json().get("results", [])
            media_type = "movie"

        if not results:
            return None

        r = results[0]
        date_field = "first_air_date" if media_type == "tv" else "release_date"
        title_field = "name" if media_type == "tv" else "title"
        return {
            "title": r.get(title_field),
            "year": (r.get(date_field) or "")[:4],
            "overview": r.get("overview", ""),
            "media_type": media_type,
            "id": r.get("id"),
        }
    except Exception as e:
        print(f"TMDb search error: {e}")
        return None


def is_recent_movie_query(query: str) -> bool:
    """True if query mentions recent/upcoming years or recency indicators."""
    current_year = datetime.now().year
    recent_years = [str(y) for y in range(2022, current_year + 2)]
    if any(y in query for y in recent_years):
        return True
    indicators = [
        "recent", "new", "latest", "upcoming", "this year", "last year",
        "coming out", "just released", "out now", "currently", "in theaters",
        "released this year", "from this year", "from last year",
    ]
    return any(ind in query.lower() for ind in indicators)


def format_vector_context(search_results: Dict[str, Any]) -> str:
    """Format ChromaDB results into a readable context block for LLM."""
    if (
        not search_results
        or not search_results.get("ids")
        or not search_results["ids"][0]
    ):
        return ""

    lines = ["**Relevant media from the database:**\n"]
    for i, movie_id in enumerate(search_results["ids"][0]):
        meta = search_results["metadatas"][0][i]
        similarity = 1 - search_results["distances"][0][i]

        media_type = meta.get("media_type", "movie")
        if media_type == "anime_tv":
            type_label = "Anime Series"
        elif media_type == "anime_movie":
            type_label = "Anime Movie"
        elif media_type == "tv":
            type_label = "TV Show"
        else:
            type_label = "Movie"

        extra = ""
        if media_type in ("tv", "anime_tv") and meta.get("number_of_seasons"):
            extra = f" ({meta['number_of_seasons']} seasons)"

        lines.append(
            f"{i+1}. **{meta.get('title', 'Unknown')}** "
            f"({meta.get('release_year', '?')}) [{type_label}{extra}]"
        )
        lines.append(f"   Genres: {meta.get('genres', 'N/A')}")
        if media_type in ("movie", "anime_movie"):
            lines.append(f"   Director: {meta.get('director', 'N/A')}")
        elif meta.get("created_by"):
            lines.append(f"   Created by: {meta['created_by']}")
        lines.append(f"   Cast: {meta.get('cast', 'N/A')}")
        lines.append(f"   Rating: {meta.get('vote_average', 'N/A')}/10")
        lines.append(f"   Relevance: {similarity:.0%}")

    return "\n".join(lines)


def get_rag_stats() -> Dict[str, Any]:
    """Return RAG system health stats."""
    if not RAG_ENABLED or vector_db is None:
        return {"enabled": False, "total_movies": 0, "status": "disabled"}
    try:
        return {
            "enabled": True,
            "total_movies": vector_db.count_movies(),
            "status": "active",
        }
    except Exception as e:
        return {"enabled": False, "total_movies": 0, "status": f"error: {e}"}


# Module-level requests session for TMDb calls (connection reuse)
_tmdb_session = None


def _get_tmdb_session():
    global _tmdb_session
    if _tmdb_session is None:
        import requests
        _tmdb_session = requests.Session()
    return _tmdb_session
