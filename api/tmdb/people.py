"""People (actor) fetchers."""
import logging
import time

from api.tmdb.cache import cached_tmdb_request
from api.tmdb.config import TMDB_API_KEY

logger = logging.getLogger(__name__)
_ACTOR_TMDB_BUDGET_SECONDS = 45


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


def _fetch_json_with_retry(label, url, max_retries, retry_delay, deadline):
    """Fetch JSON through the shared timeout and retry policy."""
    try:
        return cached_tmdb_request(
            url, max_retries=max_retries - 1, deadline=deadline
        )
    except Exception as exc:
        logger.warning("%s failed: %s", label, exc)
        raise


def _today_iso():
    """Today's date as ISO string — cutoff for including acting credits."""
    from datetime import date
    return date.today().isoformat()


def fetch_actor_details(actor_id, max_retries=3, retry_delay=2):
    deadline = time.monotonic() + _ACTOR_TMDB_BUDGET_SECONDS

    # Fetch actor details
    url = f"https://api.themoviedb.org/3/person/{actor_id}?api_key={TMDB_API_KEY}&language=en-US"
    actor_data = _fetch_json_with_retry(
        f"Actor {actor_id}", url, max_retries, retry_delay, deadline
    )
    if 'success' in actor_data and not actor_data['success']:
        raise Exception(f"TMDb API error: {actor_data.get('status_message', 'Unknown error')}")

    # Fetch movie credits
    movie_credits_url = f"https://api.themoviedb.org/3/person/{actor_id}/movie_credits?api_key={TMDB_API_KEY}&language=en-US"
    movie_credits_data = _fetch_json_with_retry(
        f"Actor {actor_id} movie credits", movie_credits_url,
        max_retries, retry_delay, deadline)

    # Fetch TV credits
    tv_credits_url = f"https://api.themoviedb.org/3/person/{actor_id}/tv_credits?api_key={TMDB_API_KEY}&language=en-US"
    tv_credits_data = _fetch_json_with_retry(
        f"Actor {actor_id} TV credits", tv_credits_url,
        max_retries, retry_delay, deadline)

    # Optional enrichment is intentionally kept out of the synchronous page path.
    tagged_images = []

    # Preserve the template contract without making another TMDB request.
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

    # Profile galleries remain supported by the template but are not fetched here.
    profile_images = []

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
