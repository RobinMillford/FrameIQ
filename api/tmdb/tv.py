"""TV list and detail fetchers."""
import hashlib
import logging
import time

import requests

from api.tmdb.cache import cached_tmdb_request
from api.tmdb.config import TMDB_API_KEY

logger = logging.getLogger(__name__)


def _show_list(url, max_shows):
    """Shared TV list-endpoint shaping: keep poster+name items."""
    data = cached_tmdb_request(url)
    results = data.get('results', [])
    filtered = [s for s in results if s.get('poster_path') and s.get('name')]
    return [
        {
            'id': show['id'],
            'name': show['name'],
            'first_air_date': show.get('first_air_date', 'N/A'),
            'poster_path': f"https://image.tmdb.org/t/p/w500{show['poster_path']}",
            'vote_average': show.get('vote_average', 0)
        } for show in filtered[:max_shows]
    ]


def fetch_airing_today_shows(max_shows=18):
    url = f"https://api.themoviedb.org/3/tv/airing_today?api_key={TMDB_API_KEY}&language=en-US&page=1"
    return _show_list(url, max_shows)


def fetch_on_the_air_shows(max_shows=18):
    url = f"https://api.themoviedb.org/3/tv/on_the_air?api_key={TMDB_API_KEY}&language=en-US&page=1"
    return _show_list(url, max_shows)


def fetch_popular_shows(max_shows=18):
    url = f"https://api.themoviedb.org/3/tv/popular?api_key={TMDB_API_KEY}&language=en-US&page=1"
    return _show_list(url, max_shows)


def fetch_shows_by_genre(genre_id, max_shows=50):
    url = f"https://api.themoviedb.org/3/discover/tv?api_key={TMDB_API_KEY}&with_genres={genre_id}&sort_by=popularity.desc&page=1"
    data = cached_tmdb_request(url)
    return data.get('results', [])[:max_shows]


def fetch_tv_show_details(show_id, max_retries=3, retry_delay=2):
    url = f"https://api.themoviedb.org/3/tv/{show_id}?api_key={TMDB_API_KEY}&language=en-US&append_to_response=credits,videos,recommendations,reviews,seasons"
    for attempt in range(max_retries):
        try:
            response = requests.get(url, timeout=5)
            response.raise_for_status()
            data = response.json()
            if 'success' in data and not data['success']:
                raise Exception(f"TMDb API error: {data.get('status_message', 'Unknown error')}")
            break
        except Exception as e:
            logger.warning("Show %s fetch attempt %s/%s failed: %s", show_id, attempt + 1, max_retries, e)
            if attempt + 1 == max_retries:
                raise Exception(f"Failed to fetch TV show details after {max_retries} retries: {e}")
            time.sleep(retry_delay)

    show = {
        'id': data.get('id'),
        'name': data.get('name'),
        'overview': data.get('overview'),
        'tagline': data.get('tagline'),
        'first_air_date': data.get('first_air_date'),
        'last_air_date': data.get('last_air_date'),
        'number_of_seasons': data.get('number_of_seasons'),
        'number_of_episodes': data.get('number_of_episodes'),
        'vote_average': round(data.get('vote_average', 0), 1),
        'vote_count': data.get('vote_count', 0),
        'status': data.get('status'),
        'original_language': data.get('original_language'),
        'poster_path': f"https://image.tmdb.org/t/p/w500{data.get('poster_path')}" if data.get('poster_path') else "https://via.placeholder.com/500x750?text=No+Image",
        'backdrop_path': f"https://image.tmdb.org/t/p/original{data.get('backdrop_path')}" if data.get('backdrop_path') else "https://via.placeholder.com/1920x1080?text=No+Backdrop",
        'genres': [genre['name'] for genre in data.get('genres', [])],
        'trailer_url': None
    }

    credits = data.get('credits', {})
    crew = credits.get('crew', [])
    for person in crew:
        if person.get('job') == 'Creator':
            show['creator'] = person.get('name')
            break

    show['cast'] = [
        {
            'id': cast_member.get('id'),
            'name': cast_member.get('name'),
            'character': cast_member.get('character'),
            'profile_path': f"https://image.tmdb.org/t/p/w185{cast_member.get('profile_path')}" if cast_member.get('profile_path') else "https://via.placeholder.com/185x278?text=No+Image"
        } for cast_member in credits.get('cast', [])[:30]
    ]

    show['seasons'] = [
        {
            'id': season.get('id'),
            'name': season.get('name'),
            'season_number': season.get('season_number'),
            'overview': season.get('overview'),
            'air_date': season.get('air_date'),
            'episode_count': season.get('episode_count'),
            'poster_path': f"https://image.tmdb.org/t/p/w500{season.get('poster_path')}" if season.get('poster_path') else "https://via.placeholder.com/500x750?text=No+Image"
        } for season in data.get('seasons', [])
    ]

    videos = data.get('videos', {}).get('results', [])
    for video in videos:
        if video.get('type') == 'Trailer' and video.get('site') == 'YouTube':
            show['trailer_url'] = f"https://www.youtube.com/embed/{video.get('key')}"
            break

    show['recommendations'] = [
        {
            'id': rec.get('id'),
            'name': rec.get('name'),
            'first_air_date': rec.get('first_air_date'),
            'poster_path': f"https://image.tmdb.org/t/p/w500{rec.get('poster_path')}" if rec.get('poster_path') else "https://via.placeholder.com/500x750?text=No+Image"
        } for rec in data.get('recommendations', {}).get('results', [])[:12]
    ]

    show['reviews'] = [
        {
            'author': review.get('author'),
            'content': review.get('content'),
            'created_at': review.get('created_at'),
            'rating': round(review.get('author_details', {}).get('rating', 0) / 2) if review.get('author_details', {}).get('rating') is not None else 0,
            'author_avatar': f"https://www.gravatar.com/avatar/{hashlib.md5(review.get('author', '').lower().encode()).hexdigest()}?s=100&d=identicon"
        } for review in data.get('reviews', {}).get('results', [])[:10]
    ]

    return show
