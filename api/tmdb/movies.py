"""Movie list and detail fetchers."""
import hashlib
import logging
from datetime import datetime

from api.tmdb.cache import cached_tmdb_request
from api.tmdb.config import TMDB_API_KEY
from api.tmdb.format import format_currency, format_runtime

logger = logging.getLogger(__name__)


def _movie_list(url, max_movies):
    """Shared list-endpoint shaping: keep poster+title items."""
    data = cached_tmdb_request(url)
    results = data.get('results', [])
    filtered = [m for m in results if m.get('poster_path') and m.get('title')]
    return [
        {
            'id': movie['id'],
            'title': movie['title'],
            'release_date': movie.get('release_date', 'N/A'),
            'poster_path': f"https://image.tmdb.org/t/p/w500{movie['poster_path']}"
        } for movie in filtered[:max_movies]
    ]


def fetch_now_playing_movies(max_movies=18):
    url = f"https://api.themoviedb.org/3/movie/now_playing?api_key={TMDB_API_KEY}&language=en-US&page=1"
    return _movie_list(url, max_movies)


def fetch_popular_movies(max_movies=18):
    url = f"https://api.themoviedb.org/3/movie/popular?api_key={TMDB_API_KEY}&language=en-US&page=1"
    return _movie_list(url, max_movies)


def fetch_upcoming_movies(max_movies=18, exclude_ids=None, current_year=None):
    if exclude_ids is None:
        exclude_ids = set()
    if current_year is None:
        current_year = datetime.now().year

    url = f"https://api.themoviedb.org/3/movie/upcoming?api_key={TMDB_API_KEY}&language=en-US&page=1"
    data = cached_tmdb_request(url)
    results = data.get('results', [])
    filtered_results = [
        movie for movie in results
        if movie.get('poster_path') and movie.get('title')
        and movie['id'] not in exclude_ids
        and movie.get('release_date', '').startswith(str(current_year))
    ]
    return [
        {
            'id': movie['id'],
            'title': movie['title'],
            'release_date': movie.get('release_date', 'N/A'),
            'poster_path': f"https://image.tmdb.org/t/p/w500{movie['poster_path']}"
        } for movie in filtered_results[:max_movies]
    ]


def fetch_trending_movies(max_movies=5):
    url = f"https://api.themoviedb.org/3/trending/movie/day?api_key={TMDB_API_KEY}"
    data = cached_tmdb_request(url)
    results = data.get('results', [])
    filtered_results = [movie for movie in results if movie.get('backdrop_path')]
    return [
        {
            'id': movie['id'],
            'title': movie['title'],
            'backdrop_path': f"https://image.tmdb.org/t/p/original{movie['backdrop_path']}"
        } for movie in filtered_results[:max_movies]
    ]


def fetch_movies_by_genre(genre_id, max_movies=50):
    url = f"https://api.themoviedb.org/3/discover/movie?api_key={TMDB_API_KEY}&with_genres={genre_id}&sort_by=popularity.desc&page=1"
    data = cached_tmdb_request(url)
    return data.get('results', [])[:max_movies]


def fetch_movie_details(movie_id, max_retries=3, retry_delay=2):
    url = f"https://api.themoviedb.org/3/movie/{movie_id}?api_key={TMDB_API_KEY}&language=en-US&append_to_response=credits,videos,recommendations,reviews"
    data = cached_tmdb_request(url, max_retries=max_retries - 1)
    if data.get('success') is False:
        raise LookupError(f"Movie {movie_id} was not found")

    logger.debug("Movie %s: %s cast members, returning first 30",
                 movie_id, len(data.get('credits', {}).get('cast', [])))

    movie = {
        'id': data.get('id'),
        'title': data.get('title'),
        'overview': data.get('overview'),
        'tagline': data.get('tagline'),
        'release_date': data.get('release_date'),
        'runtime': format_runtime(data.get('runtime')),
        'vote_average': round(data.get('vote_average', 0), 1),
        'vote_count': data.get('vote_count', 0),
        'status': data.get('status'),
        'original_language': data.get('original_language'),
        'budget': format_currency(data.get('budget')),
        'revenue': format_currency(data.get('revenue')),
        'poster_path': f"https://image.tmdb.org/t/p/w500{data.get('poster_path')}" if data.get('poster_path') else "https://via.placeholder.com/500x750?text=No+Image",
        'backdrop_path': f"https://image.tmdb.org/t/p/original{data.get('backdrop_path')}" if data.get('backdrop_path') else "https://via.placeholder.com/1920x1080?text=No+Backdrop",
        'genres': [genre['name'] for genre in data.get('genres', [])],
        'trailer_url': None,
        'certification': None
    }

    # Fetch certification
    release_url = f"https://api.themoviedb.org/3/movie/{movie_id}/release_dates?api_key={TMDB_API_KEY}"
    release_data = cached_tmdb_request(release_url, max_age=86400)
    if release_data.get('success', True):
        for result in release_data.get('results', []):
            if result.get('iso_3166_1') == 'US':
                for release in result.get('release_dates', []):
                    movie['certification'] = release.get('certification') or None
                    break
                break

    credits = data.get('credits', {})
    crew = credits.get('crew', [])
    for person in crew:
        if person.get('job') == 'Director':
            movie['director'] = person.get('name')
        elif person.get('job') in ['Screenplay', 'Writer']:
            movie['writer'] = person.get('name')

    movie['cast'] = [
        {
            'id': cast_member.get('id'),
            'name': cast_member.get('name'),
            'character': cast_member.get('character'),
            'profile_path': f"https://image.tmdb.org/t/p/w185{cast_member.get('profile_path')}" if cast_member.get('profile_path') else "https://via.placeholder.com/185x278?text=No+Image"
        } for cast_member in credits.get('cast', [])[:30]
    ]

    videos = data.get('videos', {}).get('results', [])
    for video in videos:
        if video.get('type') == 'Trailer' and video.get('site') == 'YouTube':
            movie['trailer_url'] = f"https://www.youtube.com/embed/{video.get('key')}"
            break

    movie['recommendations'] = [
        {
            'id': rec.get('id'),
            'title': rec.get('title'),
            'release_date': rec.get('release_date'),
            'poster_path': f"https://image.tmdb.org/t/p/w500{rec.get('poster_path')}" if rec.get('poster_path') else "https://via.placeholder.com/500x750?text=No+Image"
        } for rec in data.get('recommendations', {}).get('results', [])[:12]
    ]

    movie['reviews'] = [
        {
            'author': review.get('author'),
            'content': review.get('content'),
            'created_at': review.get('created_at'),
            'rating': round(review.get('author_details', {}).get('rating', 0) / 2) if review.get('author_details', {}).get('rating') is not None else 0,
            'author_avatar': f"https://www.gravatar.com/avatar/{hashlib.md5(review.get('author', '').lower().encode()).hexdigest()}?s=100&d=identicon"
        } for review in data.get('reviews', {}).get('results', [])[:10]
    ]

    return movie
