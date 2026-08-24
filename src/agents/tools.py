"""
LangChain tool wrappers for the retriever agent.

Tools:
  search_tmdb         — exact title lookup
  search_tmdb_person  — actor/director filmography
  discover_movies     — structured movie query (genre, year, language, rating, cast)
  discover_tv         — structured TV query
  get_similar_movies  — TMDb recommendations for a title
  search_tmdb_trending — trending now
  my_history          — the user's own ratings, diary and watchlist (NEW)

All TMDb calls go through api/tmdb/cache.py (bounded TTL cache shared with
the rest of the app) — no raw uncached requests.
"""

from contextvars import ContextVar
from typing import List, Dict, Any, Optional
from urllib.parse import urlencode

from dotenv import load_dotenv
from langchain_core.tools import tool

from api.tmdb.cache import cached_tmdb_request
from api.tmdb.config import TMDB_API_KEY
from api.tmdb.search import search_media as _tmdb_title_search

load_dotenv()

# Per-request user id for the my_history tool. Set by retriever_node
# (same thread as agent execution) before the agent invokes tools.
_CURRENT_USER_ID: ContextVar = ContextVar("chat_user_id", default=None)

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

_TMDB_BASE = "https://api.themoviedb.org/3"


def _tmdb_get(path: str, **params) -> Dict[str, Any]:
    """Cached TMDb GET. Returns parsed JSON (raises on network failure)."""
    query = {k: v for k, v in params.items() if v is not None}
    query["api_key"] = TMDB_API_KEY
    url = f"{_TMDB_BASE}{path}?{urlencode(query)}"
    return cached_tmdb_request(url)


@tool
def search_tmdb(title: str, year: Optional[str] = None) -> Dict[str, Any]:
    """
    Look up a specific movie or TV show on TMDb by title.

    Use for: exact title queries, recent releases, cast/director facts.

    Args:
        title:  Movie or TV show title.
        year:   Optional release year string (e.g. "2024").
    """
    try:
        # Try movies then TV via the cached search
        for media_type in ("movie", "tv"):
            results = _tmdb_title_search(title, media_type)
            if not results:
                continue
            if year:
                filtered = [r for r in results
                            if str(r.get("release_date") or r.get("first_air_date") or "").startswith(str(year))]
                results = filtered or results
            best = results[0]
            dt = best.get("release_date") or best.get("first_air_date") or ""
            return {
                "title": best.get("title") or best.get("name"),
                "year": dt[:4] or None,
                "overview": best.get("overview"),
                "media_type": media_type,
                "tmdb_id": best.get("id"),
            }
        return {
            "error": f"No results for '{title}'"
            + (f" ({year})" if year else ""),
            "suggestion": "Try alternate spelling or omit the year.",
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
        if not TMDB_API_KEY:
            return [{"error": "TMDB_API_KEY not configured"}]

        # Step 1: find person id
        people = _tmdb_get("/search/person", query=person_name, page=1).get("results", [])
        if not people:
            return [{"error": f"No person found for '{person_name}'"}]

        person = people[0]
        person_id = person["id"]
        known_name = person.get("name", person_name)

        # Step 2: fetch combined credits
        cast = _tmdb_get(f"/person/{person_id}/combined_credits").get("cast", [])

        # Sort by popularity, deduplicate by id
        seen, results = set(), []
        for item in sorted(cast, key=lambda x: x.get("popularity", 0), reverse=True):
            if item["id"] in seen:
                continue
            seen.add(item["id"])
            title = item.get("title") or item.get("name", "Unknown")
            dt = item.get("release_date") or item.get("first_air_date", "")
            results.append({
                "title": title,
                "year": dt[:4] if dt else "Unknown",
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

    Use for: "what's trending", "what's popular", "what should I watch now",
             "latest movies", "new releases this week".

    Args:
        media_type:   "movie", "tv", or "all" (default "all").
        time_window:  "day" or "week" (default "week").
    """
    try:
        if not TMDB_API_KEY:
            return [{"error": "TMDB_API_KEY not configured"}]

        items = _tmdb_get(
            f"/trending/{media_type}/{time_window}"
        ).get("results", [])[:10]

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


def _resolve_cast_id(cast_name: str) -> Optional[int]:
    """Resolve an actor name to a TMDb person id (cached)."""
    try:
        people = _tmdb_get("/search/person", query=cast_name, page=1).get("results", [])
        return people[0]["id"] if people else None
    except Exception:
        return None


@tool
def discover_movies(
    genre: Optional[str] = None,
    keywords: Optional[str] = None,
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
             "action movies with Tom Cruise", "French films of the 90s",
             vibe queries like "mind-bending heist thriller" (pass keywords).

    Args:
        genre:      Genre name, e.g. "action", "thriller", "romance", "sci-fi".
        keywords:   Comma-separated TMDb keywords capturing the VIBE or plot elements,
                    e.g. "heist,twist-ending", "time-travel", "dystopia". Derive these
                    from the user's mood or descriptive words.
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
        if not TMDB_API_KEY:
            return [{"error": "TMDB_API_KEY not configured"}]

        params: Dict[str, Any] = {
            "sort_by": sort_by,
            "include_adult": False,
            "page": 1,
            "vote_count.gte": 50,
        }

        if genre:
            gid = _resolve_genre_id(genre, is_tv=False)
            if gid:
                params["with_genres"] = gid

        if keywords:
            params["with_keywords"] = keywords

        if year_from:
            params["primary_release_date.gte"] = f"{year_from}-01-01"
        if year_to:
            params["primary_release_date.lte"] = f"{year_to}-12-31"

        if language:
            params["with_original_language"] = _resolve_lang_code(language)

        if min_rating:
            params["vote_average.gte"] = min_rating

        if cast_name:
            cast_id = _resolve_cast_id(cast_name)
            if cast_id:
                params["with_cast"] = cast_id

        items = _tmdb_get("/discover/movie", **params).get("results", [])[:top_k]

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
    keywords: Optional[str] = None,
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
             "crime TV shows from 2010s", "Spanish language series",
             vibe queries like "comfort shows about friendship" (pass keywords).

    Args:
        genre:      Genre name, e.g. "drama", "crime", "sci-fi", "animation".
        keywords:   Comma-separated TMDb keywords capturing the VIBE or themes,
                    e.g. "friendship", "survival", "dark-comedy".
        year_from:  First air date year (earliest).
        year_to:    First air date year (latest).
        language:   Original language, e.g. "korean", "japanese", "english".
        min_rating: Minimum TMDb vote average (0-10).
        sort_by:    "popularity.desc", "vote_average.desc", or "first_air_date.desc".
        top_k:      Max results (default 10).
    """
    try:
        if not TMDB_API_KEY:
            return [{"error": "TMDB_API_KEY not configured"}]

        params: Dict[str, Any] = {
            "sort_by": sort_by,
            "include_adult": False,
            "page": 1,
            "vote_count.gte": 30,
        }

        if genre:
            gid = _resolve_genre_id(genre, is_tv=True)
            if gid:
                params["with_genres"] = gid

        if keywords:
            params["with_keywords"] = keywords

        if year_from:
            params["first_air_date.gte"] = f"{year_from}-01-01"
        if year_to:
            params["first_air_date.lte"] = f"{year_to}-12-31"

        if language:
            params["with_original_language"] = _resolve_lang_code(language)

        if min_rating:
            params["vote_average.gte"] = min_rating

        items = _tmdb_get("/discover/tv", **params).get("results", [])[:top_k]

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
        if not TMDB_API_KEY:
            return [{"error": "TMDB_API_KEY not configured"}]

        # Step 1: resolve title to TMDb ID
        results = _tmdb_get(f"/search/{media_type}", query=title, page=1).get("results", [])
        if not results:
            return [{"error": f"Could not find '{title}' on TMDb"}]

        tmdb_id = results[0]["id"]
        found_title = results[0].get("title") or results[0].get("name", title)

        # Step 2: fetch recommendations
        recs = _tmdb_get(f"/{media_type}/{tmdb_id}/recommendations", page=1).get("results", [])[:10]

        if not recs:
            # fallback to /similar
            recs = _tmdb_get(f"/{media_type}/{tmdb_id}/similar").get("results", [])[:10]

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


@tool
def my_history(query: str = "recent activity") -> Dict[str, Any]:
    """
    Look up the CURRENT USER'S own viewing data: ratings, diary entries,
    watchlist, and TV shows they are tracking.

    Use for: "what did I rate Inception", "what's on my watchlist",
             "what am I currently watching", "recommend based on what I watched",
             "which genres do I watch most".

    Args:
        query:  Free-text focus hint, e.g. "ratings", "watchlist",
                "currently watching", "genres". Used to trim the response.
    """
    user_id = _CURRENT_USER_ID.get()
    if not user_id:
        return {"error": "User context unavailable — log in required."}

    try:
        # Tool runs during response streaming (outside Flask's request
        # context) — push an app context for DB access.
        from flask import has_app_context
        if not has_app_context():
            from app import app as flask_app
            with flask_app.app_context():
                return _query_user_history(user_id, query)
        return _query_user_history(user_id, query)
    except Exception as e:
        return {"error": f"my_history failed: {str(e)}"}


def _query_user_history(user_id: int, query: str) -> Dict[str, Any]:
    from models import (
        db, Review, DiaryEntry, TVShowProgress,
        user_watchlist, MediaItem,
    )

    q = query.lower()
    out: Dict[str, Any] = {}

    def _want(section: str) -> bool:
        # Return everything for broad queries; trim for specific ones.
        broad = not any(k in q for k in (
            "rating", "rated", "watchlist", "watching", "diary", "genre",
            "tracked", "show",
        ))
        return broad or section in q

    if _want("rating"):
        reviews = (
            Review.query.filter_by(user_id=user_id)
            .order_by(Review.created_at.desc()).limit(15).all()
        )
        out["recent_ratings"] = [
            {
                "title": r.media.title if r.media else "Unknown",
                "media_type": r.media_type,
                "rating_10": round(r.rating * 2, 1) if r.rating else None,
                "rated_at": r.created_at.strftime("%Y-%m-%d") if r.created_at else None,
            } for r in reviews
        ]

    if _want("diary"):
        entries = (
            DiaryEntry.query.filter_by(user_id=user_id)
            .order_by(DiaryEntry.watched_date.desc()).limit(15).all()
        )
        out["diary"] = [
            {
                "title": e.media.title if e.media else "Unknown",
                "watched_date": e.watched_date.isoformat() if e.watched_date else None,
                "is_rewatch": e.is_rewatch,
            } for e in entries
        ]

    if _want("watchlist"):
        rows = db.session.execute(
            user_watchlist.select().where(user_watchlist.c.user_id == user_id)
        ).all()
        items = []
        for row in rows[:20]:
            m = MediaItem.query.filter_by(id=row.media_id).first()
            if m:
                items.append({
                    "title": m.title, "media_type": m.media_type,
                    "priority": row.priority or "medium",
                })
        out["watchlist"] = items
        out["watchlist_count"] = len(items)

    if _want("watching") or _want("tracked") or _want("show"):
        shows = TVShowProgress.query.filter_by(user_id=user_id).limit(15).all()
        out["tv_tracking"] = [
            {
                "show_id": s.show_id,
                "status": s.status,
                "episodes_watched": s.watched_episodes,
                "episodes_total": s.total_episodes,
            } for s in shows
        ]

    # Genre preference summary from ratings
    if _want("genre"):
        genre_counts: Dict[str, int] = {}
        rated = Review.query.filter_by(user_id=user_id).limit(50).all()
        for r in rated:
            if r.media and r.media.genres:
                for g in str(r.media.genres).split(","):
                    g = g.strip()
                    if g:
                        genre_counts[g] = genre_counts.get(g, 0) + 1
        if genre_counts:
            out["favorite_genres"] = sorted(
                genre_counts.items(), key=lambda kv: kv[1], reverse=True
            )[:5]

    return out


RETRIEVER_TOOLS = [
    search_tmdb,
    search_tmdb_person,
    discover_movies,
    discover_tv,
    get_similar_movies,
    search_tmdb_trending,
    my_history,
]
