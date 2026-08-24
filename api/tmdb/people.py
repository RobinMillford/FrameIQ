"""People (actor) fetchers."""
import logging
import time

import requests

from api.tmdb.cache import cached_tmdb_request
from api.tmdb.config import TMDB_API_KEY

logger = logging.getLogger(__name__)


def fetch_trending_people(time_window='week', max_people=18):
    url = f"https://api.themoviedb.org/3/trending/person/{time_window}?api_key={TMDB_API_KEY}"
    data = cached_tmdb_request(url)
    results = data.get('results', [])
    filtered_results = [person for person in results if person.get('profile_path') and person.get('name')]
    return [
        {
            'id': person['id'],
            'name': person['name'],
            'known_for_department': person.get('known_for_department', 'N/A'),
            'profile_path': f"https://image.tmdb.org/t/p/w500{person['profile_path']}"
        } for person in filtered_results[:max_people]
    ]


def _fetch_json_with_retry(label, url, max_retries, retry_delay):
    """GET + parse JSON with retries; raises after final attempt."""
    for attempt in range(max_retries):
        try:
            response = requests.get(url, timeout=5)
            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException as e:
            logger.warning("%s attempt %s/%s failed: %s", label, attempt + 1, max_retries, e)
            if attempt + 1 == max_retries:
                raise Exception(f"Failed to fetch {label} after {max_retries} retries: {e}")
            time.sleep(retry_delay)


def _today_iso():
    """Today's date as ISO string — cutoff for including acting credits."""
    from datetime import date
    return date.today().isoformat()


def fetch_actor_details(actor_id, max_retries=3, retry_delay=2):
    # Fetch actor details
    url = f"https://api.themoviedb.org/3/person/{actor_id}?api_key={TMDB_API_KEY}&language=en-US"
    actor_data = _fetch_json_with_retry(f"Actor {actor_id}", url, max_retries, retry_delay)
    if 'success' in actor_data and not actor_data['success']:
        raise Exception(f"TMDb API error: {actor_data.get('status_message', 'Unknown error')}")

    # Fetch movie credits
    movie_credits_url = f"https://api.themoviedb.org/3/person/{actor_id}/movie_credits?api_key={TMDB_API_KEY}&language=en-US"
    movie_credits_data = _fetch_json_with_retry(
        f"Actor {actor_id} movie credits", movie_credits_url, max_retries, retry_delay)

    # Fetch TV credits
    tv_credits_url = f"https://api.themoviedb.org/3/person/{actor_id}/tv_credits?api_key={TMDB_API_KEY}&language=en-US"
    tv_credits_data = _fetch_json_with_retry(
        f"Actor {actor_id} TV credits", tv_credits_url, max_retries, retry_delay)

    # Fetch tagged images (deprecated but still functional)
    tagged_images = []
    try:
        tagged_images_url = f"https://api.themoviedb.org/3/person/{actor_id}/tagged_images?api_key={TMDB_API_KEY}"
        tagged_response = requests.get(tagged_images_url)
        if tagged_response.status_code == 200 and ('success' not in tagged_response.json() or tagged_response.json()['success']):
            tagged_data = tagged_response.json()
            seen_file_paths = set()
            tagged_images = []
            for img in sorted(tagged_data.get('results', []), key=lambda x: x.get('vote_average', 0), reverse=True):
                if img.get('file_path') and img['file_path'] not in seen_file_paths:
                    seen_file_paths.add(img['file_path'])
                    img['file_path'] = f"https://image.tmdb.org/t/p/w500{img['file_path']}"
                    tagged_images.append(img)
    except Exception as e:
        logger.warning("Actor %s tagged images fetch failed: %s", actor_id, e)

    # Fetch external IDs (with fallback)
    external_ids = {
        'facebook_id': None,
        'instagram_id': None,
        'tiktok_id': None,
        'twitter_id': None,
        'youtube_id': None,
        'imdb_id': None,
        'wikidata_id': None,
        'freebase_mid': None,
        'freebase_id': None,
        'tvrage_id': 0,
    }
    try:
        external_ids_url = f"https://api.themoviedb.org/3/person/{actor_id}/external_ids?api_key={TMDB_API_KEY}"
        external_response = requests.get(external_ids_url)
        if external_response.status_code == 200 and ('success' not in external_response.json() or external_response.json()['success']):
            external_ids_data = external_response.json()
            external_ids.update({
                'facebook_id': external_ids_data.get('facebook_id', None),
                'instagram_id': external_ids_data.get('instagram_id', None),
                'tiktok_id': external_ids_data.get('tiktok_id', None),
                'twitter_id': external_ids_data.get('twitter_id', None),
                'youtube_id': external_ids_data.get('youtube_id', None),
                'imdb_id': external_ids_data.get('imdb_id', None),
                'wikidata_id': external_ids_data.get('wikidata_id', None),
                'freebase_mid': external_ids_data.get('freebase_mid', None),
                'freebase_id': external_ids_data.get('freebase_id', None),
                'tvrage_id': external_ids_data.get('tvrage_id', 0),
            })
    except Exception as e:
        logger.warning("Actor %s external IDs fetch failed: %s", actor_id, e)

    # Fetch profile images (with fallback)
    profile_images = []
    try:
        images_url = f"https://api.themoviedb.org/3/person/{actor_id}/images?api_key={TMDB_API_KEY}"
        images_response = requests.get(images_url)
        if images_response.status_code == 200 and ('success' not in images_response.json() or images_response.json()['success']):
            images_data = images_response.json()
            profile_images = sorted(
                images_data.get('profiles', []),
                key=lambda x: x.get('vote_average', 0),
                reverse=True
            )
            for img in profile_images:
                img['file_path'] = f"https://image.tmdb.org/t/p/w500{img['file_path']}" if img.get('file_path') else "https://via.placeholder.com/500x750?text=No+Image"
    except Exception as e:
        logger.warning("Actor %s profile images fetch failed: %s", actor_id, e)

    # Process movie credits, removing duplicates by id
    movie_acting_credits = []
    seen_movie_ids = set()
    for credit in sorted(movie_credits_data.get('cast', []), key=lambda x: x.get('popularity', 0), reverse=True):
        if credit.get('id') not in seen_movie_ids and credit.get('release_date', '9999-12-31') <= _today_iso():
            seen_movie_ids.add(credit['id'])
            movie_acting_credits.append(credit)

    movie_production_credits = []
    seen_movie_prod_ids = set()
    for credit in sorted(movie_credits_data.get('crew', []), key=lambda x: x.get('popularity', 0), reverse=True):
        if credit.get('id') not in seen_movie_prod_ids:
            seen_movie_prod_ids.add(credit['id'])
            movie_production_credits.append(credit)

    # Process TV credits, removing duplicates by id
    tv_acting_credits = []
    seen_tv_ids = set()
    for credit in sorted(tv_credits_data.get('cast', []), key=lambda x: x.get('popularity', 0), reverse=True):
        if credit.get('id') not in seen_tv_ids and credit.get('first_air_date', '9999-12-31') <= _today_iso():
            seen_tv_ids.add(credit['id'])
            tv_acting_credits.append(credit)

    tv_production_credits = []
    seen_tv_prod_ids = set()
    for credit in sorted(tv_credits_data.get('crew', []), key=lambda x: x.get('popularity', 0), reverse=True):
        if credit.get('id') not in seen_tv_prod_ids:
            seen_tv_prod_ids.add(credit['id'])
            tv_production_credits.append(credit)

    # Use full lists for known_for, already deduplicated
    known_for_movies = movie_acting_credits
    known_for_tv = tv_acting_credits

    # Construct actor dictionary
    actor = {
        'name': actor_data.get('name', 'Unknown Actor'),
        'biography': actor_data.get('biography', 'No biography available.'),
        'birth_date': actor_data.get('birthday', 'N/A'),
        'place_of_birth': actor_data.get('place_of_birth', 'Unknown'),
        'gender': 'Female' if actor_data.get('gender') == 1 else 'Male' if actor_data.get('gender') == 2 else 'Unknown',
        'known_for_department': actor_data.get('known_for_department', 'N/A'),
        'known_credits': len(movie_acting_credits) + len(tv_acting_credits),
        'known_for_movies': known_for_movies,
        'known_for_tv': known_for_tv,
        'movie_acting_credits': movie_acting_credits,
        'tv_acting_credits': tv_acting_credits,
        'movie_production_credits': movie_production_credits,
        'tv_production_credits': tv_production_credits,
        'tagged_images': tagged_images,
        'profile_path': f"https://image.tmdb.org/t/p/w500{actor_data.get('profile_path')}" if actor_data.get('profile_path') else "https://via.placeholder.com/500x750?text=No+Image",
        'backdrop_path': f"https://image.tmdb.org/t/p/original{actor_data.get('profile_path')}" if actor_data.get('profile_path') else "https://via.placeholder.com/1920x1080?text=No+Backdrop",
        'also_known_as': actor_data.get('also_known_as', []),
        'popularity': actor_data.get('popularity', 0.0),
        'imdb_id': actor_data.get('imdb_id', None),
        'external_ids': external_ids,
        'profile_images': profile_images
    }

    return actor
