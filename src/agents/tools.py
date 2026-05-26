"""
LangChain tool wrappers for the retriever agent.

Tools:
  search_vector_db    — vibe/mood/theme semantic search (ChromaDB)
  search_tmdb         — exact title lookup
  search_tmdb_person  — actor/director filmography
  discover_movies     — structured movie query (genre, year, language, rating, cast)
  discover_tv         — structured TV query
  get_similar_movies  — TMDb recommendations for a title
  search_tmdb_trending — trending now
"""

import os
import requests as _requests
from typing import List, Dict, Any, Optional

# TMDb genre ID maps
_MOVIE_GENRES = {
    "action": 28, "adventure": 12, "animation": 16, "comedy": 35,
    "crime": 80, "documentary": 99, "drama": 18, "family": 10751,
    "fantasy": 14, "history": 36, "horror": 27, "music": 10402,
    "mystery": 9648, "romance": 10749, "sci-fi": 878, "science fiction": 878,
    "thriller": 53, "war": 10752, "western": 37,
}
_TV_GENRES = {
    "action": 10759, "adventure": 10759, "animation": 16, "comedy": 35,
    "crime": 80, "documentary": 99, "drama": 18, "family": 10751,
    "fantasy": 10765, "kids": 10762, "mystery": 9648, "reality": 10764,
    "sci-fi": 10765, "science fiction": 10765, "thriller": 80, "war": 10768,
    "western": 37,
}
_LANG_CODES = {
    "hindi": "hi", "bollywood": "hi", "korean": "ko", "japanese": "ja",
    "french": "fr", "spanish": "es", "italian": "it", "german": "de",
    "tamil": "ta", "telugu": "te", "chinese": "zh", "english": "en",
    "portuguese": "pt", "russian": "ru", "turkish": "tr",
}

from dotenv import load_dotenv
from langchain_core.tools import tool

from api.rag_helper import search_vector_db as _semantic_search
from api.rag_helper import search_tmdb_for_media as _tmdb_search
from api.vector_db import get_vector_db

load_dotenv()

# Shared HTTP session (connection pooling for TMDb calls).
_SESSION = _requests.Session()


def _titles_match(a: str, b: str) -> bool:
    """Loose title equality — case-insensitive, ignores leading 'the '."""
    def norm(s):
        s = s.lower().strip()
        return s[4:] if s.startswith("the ") else s
    return norm(a) == norm(b)


def _hybrid_search(query: str, top_k: int):
    """
    3-step hybrid RAG search:
      1. Semantic search on the raw query.
      2. If top result is low-confidence (<25%), enrich via TMDb overview.
      3. If an exact-title source exists, anchor semantic search to its doc.
    Returns a (results, base_similarity) tuple or (None, 0) on failure.
    """
    results = _semantic_search(query, top_k=top_k)
    if not results or not results.get("ids") or not results["ids"][0]:
        return None, 0.0

    top_sim = 1 - results["distances"][0][0]

    # Step 2: low-confidence → enrich via TMDb then re-search
    if top_sim < 0.25:
        tmdb = _tmdb_search(query)
        if tmdb:
            enriched_q = f"{tmdb['title']} {tmdb.get('overview', '')[:200]}"
            enriched = _semantic_search(enriched_q, top_k=top_k)
            if enriched and enriched.get("ids") and enriched["ids"][0]:
                results = enriched
                top_sim = 1 - results["distances"][0][0]

    # Step 3: if exact source title exists and top result doesn't match, re-anchor
    vdb = get_vector_db()
    source = vdb.search_by_exact_title(query)
    if source:
        src_title = source["metadata"].get("title", "")
        top_title = results["metadatas"][0][0].get("title", "")
        if not _titles_match(src_title, top_title):
            doc = source.get("document") or query
            anchored = _semantic_search(doc, top_k=top_k + 1)
            if anchored and anchored.get("ids") and anchored["ids"][0]:
                results = anchored
                top_sim = 1 - results["distances"][0][0]

    return results, top_sim


def _format_results(results: dict) -> List[Dict[str, Any]]:
    """Convert raw ChromaDB results to agent-friendly dicts."""
    out = []
    for i, movie_id in enumerate(results["ids"][0]):
        meta = results["metadatas"][0][i]
        similarity = 1 - results["distances"][0][i]
        out.append({
            "id": movie_id,
            "title": meta.get("title", "Unknown"),
            "year": meta.get("release_year", "Unknown"),
            "genres": meta.get("genres", "Unknown"),
            "rating": meta.get("vote_average", 0),
            "media_type": meta.get("media_type", "movie"),
            "overview": meta.get("overview", ""),
            "similarity": f"{similarity:.0%}",
        })
    return out


@tool
def search_vector_db(query: str, top_k: int = 6) -> List[Dict[str, Any]]:
    """
    Search the FrameIQ movie/TV database for semantically similar titles.

    Uses a 3-step hybrid strategy (semantic → TMDb enrichment → exact anchor).
    Use for: "movies like X", mood/theme/genre queries, vibe-based searches.

    Args:
        query:  Natural language query or source title.
        top_k:  Maximum results to return (default 6).
    """
    try:
        results, _ = _hybrid_search(query, top_k)
        if results is None:
            return []
        return _format_results(results)
    except Exception as e:
        return [{"error": f"Vector DB search failed: {str(e)}"}]


@tool
def search_tmdb(title: str, year: Optional[str] = None) -> Dict[str, Any]:
    """
    Look up a specific movie or TV show on TMDb by title.

    Use for: exact title queries, recent releases (2022+), cast/director facts.

    Args:
        title:  Movie or TV show title.
        year:   Optional release year string (e.g. "2024").
    """
    try:
        year_str = str(year) if year is not None else None
        result = _tmdb_search(title, year_str)
        if not result:
            return {
                "error": f"No results for '{title}'"
                + (f" ({year})" if year else ""),
                "suggestion": "Try alternate spelling or omit the year.",
            }
        return {
            "title": result.get("title"),
            "year": result.get("year"),
            "overview": result.get("overview"),
            "media_type": result.get("media_type"),
            "tmdb_id": result.get("id"),
        }
    except Exception as e:
        return {"error": f"TMDb lookup failed: {str(e)}"}


@tool
def search_tmdb_person(person_name: str, top_k: int = 10) -> List[Dict[str, Any]]:
    """
    Find movies and TV shows featuring a specific actor, director, or crew member.

    Use for: "movies of X", "films starring X", "X's best movies", filmography queries.

    Args:
        person_name:  Full or partial name (e.g. "Shah Rukh Khan", "SRK", "Nolan").
        top_k:        Max results to return (default 10).
    """
    try:
        api_key = os.getenv("TMDB_API_KEY")
        if not api_key:
            return [{"error": "TMDB_API_KEY not configured"}]

        # Step 1: find person id
        search_resp = _SESSION.get(
            "https://api.themoviedb.org/3/search/person",
            params={"api_key": api_key, "query": person_name, "page": 1},
            timeout=5,
        )
        people = search_resp.json().get("results", [])
        if not people:
            return [{"error": f"No person found for '{person_name}'"}]

        person = people[0]
        person_id = person["id"]
        known_name = person.get("name", person_name)

        # Step 2: fetch combined credits
        credits_resp = _SESSION.get(
            f"https://api.themoviedb.org/3/person/{person_id}/combined_credits",
            params={"api_key": api_key},
            timeout=5,
        )
        cast = credits_resp.json().get("cast", [])

        # Sort by popularity, deduplicate by id
        seen, results = set(), []
        for item in sorted(cast, key=lambda x: x.get("popularity", 0), reverse=True):
            if item["id"] in seen:
                continue
            seen.add(item["id"])
            title = item.get("title") or item.get("name", "Unknown")
            date = item.get("release_date") or item.get("first_air_date", "")
            results.append({
                "title": title,
                "year": date[:4] if date else "Unknown",
                "media_type": item.get("media_type", "movie"),
                "overview": item.get("overview", ""),
                "rating": item.get("vote_average", 0),
                "popularity": item.get("popularity", 0),
                "character": item.get("character", ""),
                "person": known_name,
            })
            if len(results) >= top_k:
                break

        return results
    except Exception as e:
        return [{"error": f"Person search failed: {str(e)}"}]


@tool
def search_tmdb_trending(
    media_type: str = "all", time_window: str = "week"
) -> List[Dict[str, Any]]:
    """
    Fetch currently trending movies/TV shows from TMDb.

    Use for: "what's trending", "what's popular", "what should I watch now".

    Args:
        media_type:   "movie", "tv", or "all" (default "all").
        time_window:  "day" or "week" (default "week").
    """
    try:
        api_key = os.getenv("TMDB_API_KEY")
        if not api_key:
            return [{"error": "TMDB_API_KEY not configured"}]

        url = f"https://api.themoviedb.org/3/trending/{media_type}/{time_window}"
        resp = _SESSION.get(url, params={"api_key": api_key}, timeout=5)
        items = resp.json().get("results", [])[:10]

        return [
            {
                "title": item.get("title") or item.get("name"),
                "year": (
                    item.get("release_date") or item.get("first_air_date", "")
                )[:4],
                "overview": item.get("overview"),
                "rating": item.get("vote_average"),
                "media_type": item.get("media_type", media_type),
                "popularity": item.get("popularity"),
            }
            for item in items
        ]
    except Exception as e:
        return [{"error": f"Trending fetch failed: {str(e)}"}]


def _resolve_genre_id(genre: str, is_tv: bool) -> Optional[int]:
    g = genre.lower().strip()
    return (_TV_GENRES if is_tv else _MOVIE_GENRES).get(g)


def _resolve_lang_code(language: str) -> str:
    return _LANG_CODES.get(language.lower().strip(), language.lower()[:2])


@tool
def discover_movies(
    genre: Optional[str] = None,
    year_from: Optional[int] = None,
    year_to: Optional[int] = None,
    language: Optional[str] = None,
    min_rating: Optional[float] = None,
    sort_by: str = "popularity.desc",
    cast_name: Optional[str] = None,
    top_k: int = 10,
) -> List[Dict[str, Any]]:
    """
    Discover movies using structured filters via TMDb.

    Use for: "best Bollywood 2023", "top Korean thrillers", "highest rated comedies",
             "action movies with Tom Cruise", "French films of the 90s".

    Args:
        genre:      Genre name, e.g. "action", "thriller", "romance", "sci-fi".
        year_from:  Earliest release year (inclusive).
        year_to:    Latest release year (inclusive).
        language:   Original language, e.g. "hindi", "korean", "english", "french".
        min_rating: Minimum TMDb vote average (0-10). Suggest 6.0+ for quality filter.
        sort_by:    One of "popularity.desc", "vote_average.desc", "revenue.desc",
                    "primary_release_date.desc". Default "popularity.desc".
        cast_name:  Filter by actor/director name (resolved to person ID automatically).
        top_k:      Max results (default 10).
    """
    try:
        api_key = os.getenv("TMDB_API_KEY")
        if not api_key:
            return [{"error": "TMDB_API_KEY not configured"}]

        params: Dict[str, Any] = {
            "api_key": api_key,
            "sort_by": sort_by,
            "include_adult": False,
            "page": 1,
            "vote_count.gte": 50,
        }

        if genre:
            gid = _resolve_genre_id(genre, is_tv=False)
            if gid:
                params["with_genres"] = gid

        if year_from:
            params["primary_release_date.gte"] = f"{year_from}-01-01"
        if year_to:
            params["primary_release_date.lte"] = f"{year_to}-12-31"

        if language:
            params["with_original_language"] = _resolve_lang_code(language)

        if min_rating:
            params["vote_average.gte"] = min_rating

        if cast_name:
            pr = _SESSION.get(
                "https://api.themoviedb.org/3/search/person",
                params={"api_key": api_key, "query": cast_name},
                timeout=5,
            )
            people = pr.json().get("results", [])
            if people:
                params["with_cast"] = people[0]["id"]

        resp = _SESSION.get(
            "https://api.themoviedb.org/3/discover/movie",
            params=params,
            timeout=5,
        )
        items = resp.json().get("results", [])[:top_k]

        return [
            {
                "title": m.get("title", "Unknown"),
                "year": (m.get("release_date") or "")[:4] or "Unknown",
                "overview": m.get("overview", ""),
                "rating": m.get("vote_average", 0),
                "popularity": m.get("popularity", 0),
                "media_type": "movie",
                "tmdb_id": m.get("id"),
            }
            for m in items
        ]
    except Exception as e:
        return [{"error": f"discover_movies failed: {str(e)}"}]


@tool
def discover_tv(
    genre: Optional[str] = None,
    year_from: Optional[int] = None,
    year_to: Optional[int] = None,
    language: Optional[str] = None,
    min_rating: Optional[float] = None,
    sort_by: str = "popularity.desc",
    top_k: int = 10,
) -> List[Dict[str, Any]]:
    """
    Discover TV shows using structured filters via TMDb.

    Use for: "best Korean dramas", "top sci-fi TV shows", "highest rated anime series",
             "crime TV shows from 2010s", "Spanish language series".

    Args:
        genre:      Genre name, e.g. "drama", "crime", "sci-fi", "animation".
        year_from:  First air date year (earliest).
        year_to:    First air date year (latest).
        language:   Original language, e.g. "korean", "japanese", "english".
        min_rating: Minimum TMDb vote average (0-10).
        sort_by:    "popularity.desc", "vote_average.desc", or "first_air_date.desc".
        top_k:      Max results (default 10).
    """
    try:
        api_key = os.getenv("TMDB_API_KEY")
        if not api_key:
            return [{"error": "TMDB_API_KEY not configured"}]

        params: Dict[str, Any] = {
            "api_key": api_key,
            "sort_by": sort_by,
            "include_adult": False,
            "page": 1,
            "vote_count.gte": 30,
        }

        if genre:
            gid = _resolve_genre_id(genre, is_tv=True)
            if gid:
                params["with_genres"] = gid

        if year_from:
            params["first_air_date.gte"] = f"{year_from}-01-01"
        if year_to:
            params["first_air_date.lte"] = f"{year_to}-12-31"

        if language:
            params["with_original_language"] = _resolve_lang_code(language)

        if min_rating:
            params["vote_average.gte"] = min_rating

        resp = _SESSION.get(
            "https://api.themoviedb.org/3/discover/tv",
            params=params,
            timeout=5,
        )
        items = resp.json().get("results", [])[:top_k]

        return [
            {
                "title": s.get("name", "Unknown"),
                "year": (s.get("first_air_date") or "")[:4] or "Unknown",
                "overview": s.get("overview", ""),
                "rating": s.get("vote_average", 0),
                "popularity": s.get("popularity", 0),
                "media_type": "tv",
                "tmdb_id": s.get("id"),
            }
            for s in items
        ]
    except Exception as e:
        return [{"error": f"discover_tv failed: {str(e)}"}]


@tool
def get_similar_movies(title: str, media_type: str = "movie") -> List[Dict[str, Any]]:
    """
    Get TMDb recommendations for a specific movie or TV show — their own similarity engine.

    Use for: "movies like Inception", "shows similar to Breaking Bad",
             "more like Parasite", "films in the same vein as Interstellar".

    Args:
        title:       Title of the source movie or TV show.
        media_type:  "movie" or "tv" (default "movie").
    """
    try:
        api_key = os.getenv("TMDB_API_KEY")
        if not api_key:
            return [{"error": "TMDB_API_KEY not configured"}]

        # Step 1: resolve title to TMDb ID
        search_url = f"https://api.themoviedb.org/3/search/{media_type}"
        sr = _SESSION.get(
            search_url,
            params={"api_key": api_key, "query": title, "page": 1},
            timeout=5,
        )
        results = sr.json().get("results", [])
        if not results:
            return [{"error": f"Could not find '{title}' on TMDb"}]

        tmdb_id = results[0]["id"]
        found_title = results[0].get("title") or results[0].get("name", title)

        # Step 2: fetch recommendations
        rec_url = f"https://api.themoviedb.org/3/{media_type}/{tmdb_id}/recommendations"
        rr = _SESSION.get(
            rec_url,
            params={"api_key": api_key, "page": 1},
            timeout=5,
        )
        recs = rr.json().get("results", [])[:10]

        if not recs:
            # fallback to /similar
            sim_url = f"https://api.themoviedb.org/3/{media_type}/{tmdb_id}/similar"
            sr2 = _SESSION.get(sim_url, params={"api_key": api_key}, timeout=5)
            recs = sr2.json().get("results", [])[:10]

        return [
            {
                "title": r.get("title") or r.get("name", "Unknown"),
                "year": (r.get("release_date") or r.get("first_air_date") or "")[:4] or "Unknown",
                "overview": r.get("overview", ""),
                "rating": r.get("vote_average", 0),
                "media_type": r.get("media_type", media_type),
                "similar_to": found_title,
            }
            for r in recs
        ]
    except Exception as e:
        return [{"error": f"get_similar_movies failed: {str(e)}"}]


RETRIEVER_TOOLS = [
    search_vector_db,
    search_tmdb,
    search_tmdb_person,
    discover_movies,
    discover_tv,
    get_similar_movies,
    search_tmdb_trending,
]
