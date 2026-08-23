"""Search and lightweight media lookup helpers."""
import logging
import time

import requests

from api.tmdb.cache import cached_tmdb_request
from api.tmdb.config import TMDB_API_KEY

logger = logging.getLogger(__name__)


def search_media(query, media_type='movie', include_adult=False):
    """Cached TMDb search.

    media_type: 'movie', 'tv', or 'person'.
    Returns the raw 'results' list (empty list on failure).
    """
    url = (
        f"https://api.themoviedb.org/3/search/{media_type}"
        f"?api_key={TMDB_API_KEY}&language=en-US&query={query}&page=1"
    )
    if include_adult:
        url += "&include_adult=true"
    try:
        data = cached_tmdb_request(url)
        return data.get('results', [])
    except Exception as e:
        logger.warning("TMDb search failed (%s, %s): %s", media_type, query, e)
        return []


def fetch_poster(id, is_movie=True, max_retries=3, retry_delay=2):
    media_type = "movie" if is_movie else "tv"
    url = f"https://api.themoviedb.org/3/{media_type}/{id}?api_key={TMDB_API_KEY}&language=en-US"
    for attempt in range(max_retries):
        try:
            response = requests.get(url, timeout=5)
            response.raise_for_status()
            data = response.json()
            poster_path = data.get('poster_path')
            return f"https://image.tmdb.org/t/p/w500{poster_path}" if poster_path else "https://via.placeholder.com/500x750?text=No+Image"
        except requests.exceptions.RequestException as e:
            logger.warning("Poster fetch attempt %s/%s failed: %s", attempt + 1, max_retries, e)
            time.sleep(retry_delay)
    logger.error("Poster fetch failed for %s/%s after %s retries", media_type, id, max_retries)
    return "https://via.placeholder.com/500x750?text=No+Image"


def fetch_tmdb_recommendations(id, is_movie=True, max_recommendations=50):
    media_type = "movie" if is_movie else "tv"
    url = f"https://api.themoviedb.org/3/{media_type}/{id}/recommendations?api_key={TMDB_API_KEY}&language=en-US&page=1"
    data = cached_tmdb_request(url)
    return data.get('results', [])[:max_recommendations]


def fetch_media_details(media_type, media_id):
    """Lightweight cached TMDb details for movie/tv (no append_to_response).

    Returns dict or None if unavailable.
    """
    url = f"https://api.themoviedb.org/3/{media_type}/{media_id}?api_key={TMDB_API_KEY}&language=en-US"
    try:
        data = cached_tmdb_request(url)
    except Exception as e:
        logger.warning("TMDb details fetch failed (%s/%s): %s", media_type, media_id, e)
        return None
    if data.get('success') is False:
        return None
    return data
