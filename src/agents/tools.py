"""
LangChain tool wrappers for the retriever agent.

These tools wrap existing FrameIQ functionality (ChromaDB, TMDb API) 
as LangChain tools that can be bound to LLMs.
"""

from langchain_core.tools import tool
from typing import List, Dict, Any, Optional
import os
import requests
from dotenv import load_dotenv

# Import existing functionality
from api.rag_helper import search_vector_db as _search_vector_db
from api.rag_helper import search_tmdb_for_media as _search_tmdb_for_media

load_dotenv()


@tool
def search_vector_db(query: str, top_k: int = 5) -> List[Dict[str, Any]]:
    """
    Search the ChromaDB vector database for semantically similar movies/TV shows.
    
    Use this tool when:
    - User asks for recommendations based on vibes, themes, or plot similarity
    - User wants movies "like" another movie
    - User describes a mood or genre without specific titles
    
    Args:
        query: Natural language search query (e.g., "dark psychological thrillers")
        top_k: Number of results to return (default: 5)
    
    Returns:
        List of movie/TV metadata dictionaries with titles, years, genres, ratings
    """
    try:
        results = _search_vector_db(query, top_k=top_k)
        
        if not results or not results.get('ids') or not results['ids'][0]:
            return []
        
        # Format results for the agent
        formatted_results = []
        for i, movie_id in enumerate(results['ids'][0]):
            metadata = results['metadatas'][0][i]
            similarity = 1 - results['distances'][0][i]
            
            formatted_results.append({
                'id': movie_id,
                'title': metadata.get('title', 'Unknown'),
                'year': metadata.get('release_year', 'Unknown'),
                'genres': metadata.get('genres', 'Unknown'),
                'rating': metadata.get('vote_average', 0),
                'media_type': metadata.get('media_type', 'movie'),
                'overview': metadata.get('overview', ''),
                'similarity': f"{similarity:.2%}"
            })
        
        return formatted_results
    
    except Exception as e:
        return [{"error": f"Vector DB search failed: {str(e)}"}]


@tool
def search_tmdb(title: str, year: Optional[str] = None) -> Dict[str, Any]:
    """
    Search The Movie Database (TMDb) API for specific movie/TV show information.
    
    Use this tool when:
    - User asks about a specific title by name
    - User wants recent/new releases (2022-2025) that might not be in vector DB
    - User asks for factual metadata (release date, cast, director)
    
    Args:
        title: Movie or TV show title to search for
        year: Optional release year (as string, e.g., "2024")
    
    Returns:
        Dictionary with movie/TV metadata (title, year, overview, genres, etc.)
    """
    try:
        # Convert year to string if it's an integer
        year_str = str(year) if year is not None else None
        result = _search_tmdb_for_media(title, year_str)
        
        if not result:
            return {
                "error": f"No results found for '{title}'" + (f" ({year})" if year else ""),
                "suggestion": "Try a different spelling or check if it's a TV show vs movie"
            }
        
        return {
            'title': result.get('title'),
            'year': result.get('year'),
            'overview': result.get('overview'),
            'media_type': result.get('media_type'),
            'tmdb_id': result.get('id')
        }
    
    except Exception as e:
        return {"error": f"TMDb API search failed: {str(e)}"}


@tool
def search_tmdb_trending(media_type: str = "all", time_window: str = "week") -> List[Dict[str, Any]]:
    """
    Get trending movies/TV shows from TMDb.
    
    Use this tool when:
    - User asks "what's trending", "what's popular now", "what's hot"
    - User wants current recommendations without specific criteria
    
    Args:
        media_type: "movie", "tv", or "all" (default: "all")
        time_window: "day" or "week" (default: "week")
    
    Returns:
        List of trending media with titles, ratings, and overviews
    """
    try:
        TMDB_API_KEY = os.getenv("TMDB_API_KEY")
        if not TMDB_API_KEY:
            return [{"error": "TMDb API key not configured"}]
        
        url = f"https://api.themoviedb.org/3/trending/{media_type}/{time_window}"
        params = {"api_key": TMDB_API_KEY}
        
        response = requests.get(url, params=params, timeout=5)
        results = response.json().get("results", [])[:10]  # Top 10
        
        trending = []
        for item in results:
            trending.append({
                'title': item.get('title') or item.get('name'),
                'year': (item.get('release_date') or item.get('first_air_date', ''))[:4],
                'overview': item.get('overview'),
                'rating': item.get('vote_average'),
                'media_type': item.get('media_type', media_type),
                'popularity': item.get('popularity')
            })
        
        return trending
    
    except Exception as e:
        return [{"error": f"Trending search failed: {str(e)}"}]


# Export all tools as a list for easy binding
RETRIEVER_TOOLS = [search_vector_db, search_tmdb, search_tmdb_trending]
