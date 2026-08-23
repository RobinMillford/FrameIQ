"""Legacy import path for the TMDb client — kept as a compatibility shim.

All implementation now lives in the api.tmdb package. Both
`from api.tmdb_client import X` and `from api.tmdb import X` work.
"""
from api.tmdb import *  # noqa: F401,F403
from api.tmdb import (  # noqa: F401  — explicit re-export incl. private names
    TMDB_API_KEY,
    _BoundedTTLCache,
    tmdb_cache,
    get_cache_key,
    cached_tmdb_request,
    format_runtime,
    format_currency,
    fetch_now_playing_movies,
    fetch_popular_movies,
    fetch_upcoming_movies,
    fetch_trending_movies,
    fetch_movies_by_genre,
    fetch_movie_details,
    fetch_airing_today_shows,
    fetch_on_the_air_shows,
    fetch_popular_shows,
    fetch_shows_by_genre,
    fetch_tv_show_details,
    fetch_trending_people,
    fetch_actor_details,
    search_media,
    fetch_poster,
    fetch_tmdb_recommendations,
    fetch_media_details,
)
