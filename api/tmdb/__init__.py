"""
FrameIQ TMDb client package.

All public names are re-exported here so `from api.tmdb import X` works,
and the legacy module path `api.tmdb_client` remains a working alias.

Submodules:
    config  — TMDb API key
    cache   — bounded TTL cache + cached_tmdb_request
    format  — display formatting helpers
    movies  — movie list/detail fetchers
    tv      — TV list/detail fetchers
    people  — trending people + actor details
    search  — search + lightweight lookups
"""

from api.tmdb.config import TMDB_API_KEY                     # noqa: F401
from api.tmdb.cache import (                                 # noqa: F401
    _BoundedTTLCache,
    tmdb_cache,
    get_cache_key,
    cached_tmdb_request,
)
from api.tmdb.format import (                                # noqa: F401
    format_runtime,
    format_currency,
)
from api.tmdb.movies import (                                # noqa: F401
    fetch_now_playing_movies,
    fetch_popular_movies,
    fetch_upcoming_movies,
    fetch_trending_movies,
    fetch_movies_by_genre,
    fetch_movie_details,
)
from api.tmdb.tv import (                                    # noqa: F401
    fetch_airing_today_shows,
    fetch_on_the_air_shows,
    fetch_popular_shows,
    fetch_shows_by_genre,
    fetch_tv_show_details,
)
from api.tmdb.people import (                                # noqa: F401
    fetch_trending_people,
    fetch_actor_details,
)
from api.tmdb.search import (                                # noqa: F401
    search_media,
    fetch_poster,
    fetch_tmdb_recommendations,
    fetch_media_details,
)

__all__ = [
    'TMDB_API_KEY',
    '_BoundedTTLCache', 'tmdb_cache', 'get_cache_key', 'cached_tmdb_request',
    'format_runtime', 'format_currency',
    'fetch_now_playing_movies', 'fetch_popular_movies', 'fetch_upcoming_movies',
    'fetch_trending_movies', 'fetch_movies_by_genre', 'fetch_movie_details',
    'fetch_airing_today_shows', 'fetch_on_the_air_shows', 'fetch_popular_shows',
    'fetch_shows_by_genre', 'fetch_tv_show_details',
    'fetch_trending_people', 'fetch_actor_details',
    'search_media', 'fetch_poster', 'fetch_tmdb_recommendations',
    'fetch_media_details',
]
