import requests
import time
import os
from dotenv import load_dotenv
from datetime import datetime
import hashlib
from functools import lru_cache

# Load environment variables
load_dotenv()

TMDB_API_KEY = os.getenv("TMDB_API_KEY")
TMDB_API_KEY_2 = os.getenv("TMDB_API_KEY_2")

# Simple in-memory cache for TMDB data
tmdb_cache = {}

def get_cache_key(*args):
    """Generate a cache key from arguments"""
    return hashlib.md5(str(args).encode()).hexdigest()

def cached_tmdb_request(url, max_age=3600):
    """Make a TMDB request with caching"""
    cache_key = get_cache_key(url)
    current_time = time.time()
    
    # Check if we have a cached response that's still valid
    if cache_key in tmdb_cache:
        cached_data, timestamp = tmdb_cache[cache_key]
        if current_time - timestamp < max_age:
            print(f"Cache hit for {url}")
            return cached_data
    
    # Make the request
    print(f"Making request to {url}")
    response = requests.get(url)
    data = response.json()
    
    # Cache the response
    tmdb_cache[cache_key] = (data, current_time)
    return data

def fetch_now_playing_movies(max_movies=18):
    url = f"https://api.themoviedb.org/3/movie/now_playing?api_key={TMDB_API_KEY}&language=en-US&page=1"
    data = cached_tmdb_request(url)
    results = data.get('results', [])
    filtered_results = [movie for movie in results if movie.get('poster_path') and movie.get('title')]
    return [
        {
            'id': movie['id'],
            'title': movie['title'],
            'release_date': movie.get('release_date', 'N/A'),
            'poster_path': f"https://image.tmdb.org/t/p/w500{movie['poster_path']}"
        } for movie in filtered_results[:max_movies]
    ]

def fetch_popular_movies(max_movies=18):
    url = f"https://api.themoviedb.org/3/movie/popular?api_key={TMDB_API_KEY}&language=en-US&page=1"
    data = cached_tmdb_request(url)
    results = data.get('results', [])
    filtered_results = [movie for movie in results if movie.get('poster_path') and movie.get('title')]
    return [
        {
            'id': movie['id'],
            'title': movie['title'],
            'release_date': movie.get('release_date', 'N/A'),
            'poster_path': f"https://image.tmdb.org/t/p/w500{movie['poster_path']}"
        } for movie in filtered_results[:max_movies]
    ]

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
        if movie.get('poster_path') and movie.get('title') and 
           movie['id'] not in exclude_ids and  # Exclude movies from now_playing and popular
           movie.get('release_date', '').startswith(str(current_year))  # Only current year
    ]
    return [
        {
            'id': movie['id'],
            'title': movie['title'],
            'release_date': movie.get('release_date', 'N/A'),
            'poster_path': f"https://image.tmdb.org/t/p/w500{movie['poster_path']}"
        } for movie in filtered_results[:max_movies]
    ]

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

def fetch_airing_today_shows(max_shows=18):
    url = f"https://api.themoviedb.org/3/tv/airing_today?api_key={TMDB_API_KEY}&language=en-US&page=1"
    data = cached_tmdb_request(url)
    results = data.get('results', [])
    filtered_results = [show for show in results if show.get('poster_path') and show.get('name')]
    return [
        {
            'id': show['id'],
            'name': show['name'],
            'first_air_date': show.get('first_air_date', 'N/A'),
            'poster_path': f"https://image.tmdb.org/t/p/w500{show['poster_path']}",
            'vote_average': show.get('vote_average', 0)
        } for show in filtered_results[:max_shows]
    ]

def fetch_on_the_air_shows(max_shows=18):
    url = f"https://api.themoviedb.org/3/tv/on_the_air?api_key={TMDB_API_KEY}&language=en-US&page=1"
    data = cached_tmdb_request(url)
    results = data.get('results', [])
    filtered_results = [show for show in results if show.get('poster_path') and show.get('name')]
    return [
        {
            'id': show['id'],
            'name': show['name'],
            'first_air_date': show.get('first_air_date', 'N/A'),
            'poster_path': f"https://image.tmdb.org/t/p/w500{show['poster_path']}",
            'vote_average': show.get('vote_average', 0)
        } for show in filtered_results[:max_shows]
    ]

def fetch_popular_shows(max_shows=18):
    url = f"https://api.themoviedb.org/3/tv/popular?api_key={TMDB_API_KEY}&language=en-US&page=1"
    data = cached_tmdb_request(url)
    results = data.get('results', [])
    filtered_results = [show for show in results if show.get('poster_path') and show.get('name')]
    return [
        {
            'id': show['id'],
            'name': show['name'],
            'first_air_date': show.get('first_air_date', 'N/A'),
            'poster_path': f"https://image.tmdb.org/t/p/w500{show['poster_path']}",
            'vote_average': show.get('vote_average', 0)
        } for show in filtered_results[:max_shows]
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

def fetch_shows_by_genre(genre_id, max_shows=50):
    url = f"https://api.themoviedb.org/3/discover/tv?api_key={TMDB_API_KEY}&with_genres={genre_id}&sort_by=popularity.desc&page=1"
    data = cached_tmdb_request(url)
    return data.get('results', [])[:max_shows]

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
            print(f"Error fetching poster (attempt {attempt + 1}/{max_retries}): {e}")
            time.sleep(retry_delay)
    print(f"Failed to fetch poster for {media_type}/{id} after {max_retries} retries.")
    return "https://via.placeholder.com/500x750?text=No+Image"

def fetch_tmdb_recommendations(id, is_movie=True, max_recommendations=50):
    media_type = "movie" if is_movie else "tv"
    url = f"https://api.themoviedb.org/3/{media_type}/{id}/recommendations?api_key={TMDB_API_KEY}&language=en-US&page=1"
    time.sleep(1)
    data = cached_tmdb_request(url)
    return data.get('results', [])[:max_recommendations]

def fetch_movie_details(movie_id, max_retries=3, retry_delay=2):
    url = f"https://api.themoviedb.org/3/movie/{movie_id}?api_key={TMDB_API_KEY}&language=en-US&append_to_response=credits,videos,recommendations,reviews"
    for attempt in range(max_retries):
        try:
            response = requests.get(url, timeout=5)
            response.raise_for_status()
            data = response.json()
            if 'success' in data and not data['success']:
                raise Exception(f"TMDb API error: {data.get('status_message', 'Unknown error')}")
            break
        except (requests.exceptions.RequestException, Exception) as e:
            print(f"Attempt {attempt + 1}/{max_retries} failed for movie {movie_id}: {e}")
            if attempt + 1 == max_retries:
                raise Exception(f"Failed to fetch movie details after {max_retries} retries: {e}")
            time.sleep(retry_delay)
    
    print(f"Movie {movie_id} fetching {len(data.get('credits', {}).get('cast', []))} total cast members, returning first 30")
    
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
    release_response = requests.get(release_url)
    if release_response.status_code == 200:
        release_data = release_response.json()
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
        except (requests.exceptions.RequestException, Exception) as e:
            print(f"Attempt {attempt + 1}/{max_retries} failed for show {show_id}: {e}")
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

def fetch_actor_details(actor_id, max_retries=3, retry_delay=2):
    # Fetch actor details
    url = f"https://api.themoviedb.org/3/person/{actor_id}?api_key={TMDB_API_KEY}&language=en-US"
    for attempt in range(max_retries):
        try:
            response = requests.get(url, timeout=5)
            response.raise_for_status()
            data = response.json()
            if 'success' in data and not data['success']:
                raise Exception(f"TMDb API error: {data.get('status_message', 'Unknown error')}")
            break
        except (requests.exceptions.RequestException, Exception) as e:
            print(f"Attempt {attempt + 1}/{max_retries} failed for actor {actor_id}: {e}")
            if attempt + 1 == max_retries:
                raise Exception(f"Failed to fetch actor details after {max_retries} retries: {e}")
            time.sleep(retry_delay)
    
    actor_data = data

    # Fetch movie credits
    movie_credits_url = f"https://api.themoviedb.org/3/person/{actor_id}/movie_credits?api_key={TMDB_API_KEY}&language=en-US"
    for attempt in range(max_retries):
        try:
            movie_response = requests.get(movie_credits_url, timeout=5)
            movie_response.raise_for_status()
            movie_credits_data = movie_response.json()
            break
        except requests.exceptions.RequestException as e:
            print(f"Attempt {attempt + 1}/{max_retries} failed for movie credits of actor {actor_id}: {e}")
            if attempt + 1 == max_retries:
                raise Exception(f"Failed to fetch movie credits after {max_retries} retries: {e}")
            time.sleep(retry_delay)

    # Fetch TV credits
    tv_credits_url = f"https://api.themoviedb.org/3/person/{actor_id}/tv_credits?api_key={TMDB_API_KEY}&language=en-US"
    for attempt in range(max_retries):
        try:
            tv_response = requests.get(tv_credits_url, timeout=5)
            tv_response.raise_for_status()
            tv_credits_data = tv_response.json()
            break
        except requests.exceptions.RequestException as e:
            print(f"Attempt {attempt + 1}/{max_retries} failed for TV credits of actor {actor_id}: {e}")
            if attempt + 1 == max_retries:
                raise Exception(f"Failed to fetch TV credits after {max_retries} retries: {e}")
            time.sleep(retry_delay)

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
        print(f"Failed to fetch tagged images for actor {actor_id}: {e}")

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
        print(f"Failed to fetch external IDs for actor {actor_id}: {e}")

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
        print(f"Failed to fetch profile images for actor {actor_id}: {e}")

    # Process movie credits, removing duplicates by id
    movie_acting_credits = []
    seen_movie_ids = set()
    for credit in sorted(movie_credits_data.get('cast', []), key=lambda x: x.get('popularity', 0), reverse=True):
        if credit.get('id') not in seen_movie_ids and credit.get('release_date', '9999-12-31') <= '2025-04-08':
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
        if credit.get('id') not in seen_tv_ids and credit.get('first_air_date', '9999-12-31') <= '2025-04-08':
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

def format_runtime(minutes):
    if not minutes:
        return "N/A"
    hours = minutes // 60
    remaining_minutes = minutes % 60
    return f"{hours}h {remaining_minutes}m" if hours > 0 else f"{remaining_minutes}m"

def format_currency(amount):
    if not amount:
        return "N/A"
    return f"{amount:,}"