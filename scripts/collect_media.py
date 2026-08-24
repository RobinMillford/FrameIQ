"""
Comprehensive Media Data Collector from TMDb API
Fetches ALL available information for movies and TV shows for rich embeddings
"""

import requests
import os
from datetime import datetime
import json
import time
from typing import List, Dict, Any, Optional
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

TMDB_API_KEY = os.getenv("TMDB_API_KEY")
BASE_URL = "https://api.themoviedb.org/3"

# Rate limiting
REQUEST_DELAY = 0.25  # 4 requests per second (TMDb limit)


def make_request(url: str, params: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Make a request to TMDb API with rate limiting and error handling."""
    try:
        time.sleep(REQUEST_DELAY)  # Rate limiting
        response = requests.get(url, params=params, timeout=10)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        print(f"Error making request to {url}: {e}")
        return None


def is_anime(media_data: Dict[str, Any]) -> bool:
    """
    Detect if media is anime based on origin country, keywords, and genres.
    
    Args:
        media_data: Media data dictionary from TMDb
    
    Returns:
        True if media is anime, False otherwise
    """
    # Check production countries
    countries = media_data.get('production_countries', [])
    is_japanese = any('Japan' in country.get('name', '') if isinstance(country, dict) else 'Japan' in str(country) for country in countries)
    
    # Check keywords
    keywords = media_data.get('keywords', {})
    if isinstance(keywords, dict):
        keyword_list = keywords.get('keywords', []) or keywords.get('results', [])
    else:
        keyword_list = keywords
    
    anime_keywords = ['anime', 'manga', 'japanese animation', 'shounen', 'shoujo', 'seinen']
    has_anime_keyword = any(
        any(kw_term in str(kw.get('name', '')).lower() if isinstance(kw, dict) else kw_term in str(kw).lower() 
            for kw_term in anime_keywords)
        for kw in keyword_list
    )
    
    # Check genres (Animation + Japanese origin is strong indicator)
    genres = media_data.get('genres', [])
    is_animation = any(
        'Animation' in genre.get('name', '') if isinstance(genre, dict) else 'Animation' in str(genre)
        for genre in genres
    )
    
    # Check original language
    is_japanese_lang = media_data.get('original_language') == 'ja'
    
    # Anime if: (Japanese origin OR Japanese language) AND (Animation OR anime keywords)
    return (is_japanese or is_japanese_lang) and (is_animation or has_anime_keyword)


def fetch_recent_movie_ids(years_back: int = 3, max_pages: int = 10) -> List[int]:
    """Fetch movie IDs from recent years."""
    movie_ids = set()
    current_year = datetime.now().year
    start_year = current_year - years_back
    
    print(f"Fetching movies from {start_year} to {current_year + 1}...")
    
    for year in range(start_year, current_year + 2):
        print(f"\n  Fetching movies from {year}...")
        for page in range(1, max_pages + 1):
            url = f"{BASE_URL}/discover/movie"
            params = {
                "api_key": TMDB_API_KEY,
                "primary_release_year": year,
                "sort_by": "popularity.desc",
                "page": page,
                "include_adult": False,
                "vote_count.gte": 10
            }
            
            data = make_request(url, params)
            if not data or 'results' not in data: break
            
            results = data['results']
            if not results: break
            
            for movie in results:
                movie_ids.add(movie['id'])
            
            print(f"    Page {page}: {len(results)} movies (Total: {len(movie_ids)})")
            if page >= data.get('total_pages', 0): break
    
    return list(movie_ids)


def fetch_recent_tv_ids(years_back: int = 3, max_pages: int = 10) -> List[int]:
    """Fetch TV show IDs from recent years."""
    tv_ids = set()
    current_year = datetime.now().year
    start_year = current_year - years_back
    
    print(f"Fetching TV shows from {start_year} to {current_year + 1}...")
    
    for year in range(start_year, current_year + 2):
        print(f"\n  Fetching TV shows from {year}...")
        for page in range(1, max_pages + 1):
            url = f"{BASE_URL}/discover/tv"
            params = {
                "api_key": TMDB_API_KEY,
                "first_air_date_year": year,
                "sort_by": "popularity.desc",
                "page": page,
                "include_adult": False,
                "vote_count.gte": 10
            }
            
            data = make_request(url, params)
            if not data or 'results' not in data: break
            
            results = data['results']
            if not results: break
            
            for show in results:
                tv_ids.add(show['id'])
            
            print(f"    Page {page}: {len(results)} shows (Total: {len(tv_ids)})")
            if page >= data.get('total_pages', 0): break
    
    return list(tv_ids)


def fetch_comprehensive_movie_data(movie_id: int) -> Optional[Dict[str, Any]]:
    """Fetch ALL available data for a movie from TMDb."""
    url = f"{BASE_URL}/movie/{movie_id}"
    params = {
        "api_key": TMDB_API_KEY,
        "append_to_response": "credits,keywords,reviews,videos,similar,recommendations,release_dates,alternative_titles"
    }
    
    data = make_request(url, params)
    if not data or 'id' not in data: return None
    
    # Extract comprehensive information
    movie_data = {
        'id': data.get('id'),
        'title': data.get('title'),
        'original_title': data.get('original_title'),
        'tagline': data.get('tagline', ''),
        'overview': data.get('overview', ''),
        'release_date': data.get('release_date', ''),
        'runtime': data.get('runtime', 0),
        'status': data.get('status', ''),
        'vote_average': data.get('vote_average', 0),
        'vote_count': data.get('vote_count', 0),
        'popularity': data.get('popularity', 0),
        'budget': data.get('budget', 0),
        'revenue': data.get('revenue', 0),
        'original_language': data.get('original_language', ''),
        'spoken_languages': [lang['english_name'] for lang in data.get('spoken_languages', [])],
        'production_countries': [country['name'] for country in data.get('production_countries', [])],
        'genres': [genre['name'] for genre in data.get('genres', [])],
        'keywords': [kw['name'] for kw in data.get('keywords', {}).get('keywords', [])],
        'production_companies': [company['name'] for company in data.get('production_companies', [])],
        'belongs_to_collection': data.get('belongs_to_collection', {}).get('id') if data.get('belongs_to_collection') else None,
        'collection_name': data.get('belongs_to_collection', {}).get('name') if data.get('belongs_to_collection') else None,
    }
    
    # Detect if anime and set media_type accordingly
    if is_anime(data):
        movie_data['media_type'] = 'anime_movie'
    else:
        movie_data['media_type'] = 'movie'
    
    # Credits
    credits = data.get('credits', {})
    cast = credits.get('cast', [])[:15]
    movie_data['cast'] = [{'name': m['name'], 'character': m.get('character', ''), 'order': m.get('order', 999)} for m in cast]
    
    crew = credits.get('crew', [])
    movie_data['director'] = [m['name'] for m in crew if m.get('job') == 'Director']
    movie_data['writers'] = list(set([m['name'] for m in crew if m.get('job') in ['Screenplay', 'Writer', 'Story']]))
    movie_data['producers'] = list(set([m['name'] for m in crew if m.get('job') in ['Producer', 'Executive Producer']]))
    movie_data['cinematographer'] = [m['name'] for m in crew if m.get('job') == 'Director of Photography']
    movie_data['composer'] = [m['name'] for m in crew if m.get('job') == 'Original Music Composer']
    
    # Certification
    release_dates = data.get('release_dates', {}).get('results', [])
    movie_data['certification'] = 'NR'
    for release in release_dates:
        if release.get('iso_3166_1') == 'US':
            for date_info in release.get('release_dates', []):
                if date_info.get('certification'):
                    movie_data['certification'] = date_info.get('certification')
                    break
            break
            
    # Reviews
    reviews = data.get('reviews', {}).get('results', [])[:3]
    movie_data['reviews'] = [{'author': r['author'], 'content': r['content'][:500], 'rating': r.get('author_details', {}).get('rating')} for r in reviews]
    
    # Similar & Recommended
    movie_data['similar_movies'] = [{'id': m['id'], 'title': m['title']} for m in data.get('similar', {}).get('results', [])[:10]]
    movie_data['recommended_movies'] = [{'id': m['id'], 'title': m['title']} for m in data.get('recommendations', {}).get('results', [])[:10]]
    
    # Alt titles
    movie_data['alternative_titles'] = [t['title'] for t in data.get('alternative_titles', {}).get('titles', []) if t.get('iso_3166_1') == 'US'][:5]
    
    return movie_data


def fetch_comprehensive_tv_data(tv_id: int) -> Optional[Dict[str, Any]]:
    """Fetch ALL available data for a TV show from TMDb."""
    url = f"{BASE_URL}/tv/{tv_id}"
    params = {
        "api_key": TMDB_API_KEY,
        "append_to_response": "credits,keywords,reviews,videos,similar,recommendations,content_ratings,alternative_titles"
    }
    
    data = make_request(url, params)
    if not data or 'id' not in data: return None
    
    # Extract comprehensive information
    tv_data = {
        'id': data.get('id'),
        'title': data.get('name'),  # Map 'name' to 'title' for consistency
        'original_title': data.get('original_name'),
        'tagline': data.get('tagline', ''),
        'overview': data.get('overview', ''),
        'release_date': data.get('first_air_date', ''),  # Map 'first_air_date' to 'release_date'
        'last_air_date': data.get('last_air_date', ''),
        'status': data.get('status', ''),
        'number_of_seasons': data.get('number_of_seasons', 0),
        'number_of_episodes': data.get('number_of_episodes', 0),
        'vote_average': data.get('vote_average', 0),
        'vote_count': data.get('vote_count', 0),
        'popularity': data.get('popularity', 0),
        'original_language': data.get('original_language', ''),
        'spoken_languages': [lang['english_name'] for lang in data.get('spoken_languages', [])],
        'production_countries': [country['name'] for country in data.get('production_countries', [])],
        'genres': [genre['name'] for genre in data.get('genres', [])],
        'keywords': [kw['name'] for kw in data.get('keywords', {}).get('results', [])],
        'production_companies': [company['name'] for company in data.get('production_companies', [])],
        'created_by': [creator['name'] for creator in data.get('created_by', [])],
        'networks': [network['name'] for network in data.get('networks', [])],
    }
    
    # Detect if anime and set media_type accordingly
    if is_anime(data):
        tv_data['media_type'] = 'anime_tv'
    else:
        tv_data['media_type'] = 'tv'
    
    # Credits
    credits = data.get('credits', {})
    cast = credits.get('cast', [])[:15]
    tv_data['cast'] = [{'name': m['name'], 'character': m.get('character', ''), 'order': m.get('order', 999)} for m in cast]
    
    crew = credits.get('crew', [])
    # TV shows often list creators separately, but we can check crew too
    tv_data['director'] = [m['name'] for m in crew if m.get('job') == 'Director'] # Often empty for whole show
    tv_data['writers'] = list(set([m['name'] for m in crew if m.get('job') in ['Screenplay', 'Writer', 'Story']]))
    
    # Certification
    content_ratings = data.get('content_ratings', {}).get('results', [])
    tv_data['certification'] = 'NR'
    for rating in content_ratings:
        if rating.get('iso_3166_1') == 'US':
            tv_data['certification'] = rating.get('rating')
            break
            
    # Reviews
    reviews = data.get('reviews', {}).get('results', [])[:3]
    tv_data['reviews'] = [{'author': r['author'], 'content': r['content'][:500], 'rating': r.get('author_details', {}).get('rating')} for r in reviews]
    
    # Similar & Recommended
    tv_data['similar_movies'] = [{'id': m['id'], 'title': m['name']} for m in data.get('similar', {}).get('results', [])[:10]] # Keep key 'similar_movies' for compatibility
    tv_data['recommended_movies'] = [{'id': m['id'], 'title': m['name']} for m in data.get('recommendations', {}).get('results', [])[:10]]
    
    return tv_data


def collect_media(
    years_back: int = 3,
    max_pages_per_year: int = 10,
    output_file: str = "data/movies.json"
) -> List[Dict[str, Any]]:
    """
    Main function to collect comprehensive media data.
    Saves to local JSON file which will be used by generate_embeddings.py
    to upload to Chroma Cloud.
    """
    print("=" * 70)
    print("COMPREHENSIVE MEDIA DATA COLLECTION")
    print("=" * 70)
    
    # Load existing data if it exists (for deduplication)
    print("\n[Step 1/4] Loading existing data...")
    existing_media = {}
    if os.path.exists(output_file):
        with open(output_file, 'r', encoding='utf-8') as f:
            existing_list = json.load(f)
            # Index by composite ID
            for item in existing_list:
                media_type = item.get('media_type', 'movie')
                composite_id = f"{media_type}_{item['id']}"
                existing_media[composite_id] = item
        print(f"✓ Loaded {len(existing_media)} existing items")
    else:
        print("✓ No existing data found, starting fresh")
    
    # Step 2: Fetch IDs
    print("\n[Step 2/4] Fetching media IDs...")
    movie_ids = fetch_recent_movie_ids(years_back, max_pages_per_year)
    tv_ids = fetch_recent_tv_ids(years_back, max_pages_per_year)
    
    # Step 3: Fetch details
    all_media = []
    
    # Movies
    print(f"\n[Step 3/4] Fetching data for {len(movie_ids)} movies...")
    for i, movie_id in enumerate(movie_ids, 1):
        if i % 50 == 0: print(f"  Progress: {i}/{len(movie_ids)}")
        data = fetch_comprehensive_movie_data(movie_id)
        if data: all_media.append(data)
        
    # TV Shows
    print(f"\n[Step 3/4] Fetching data for {len(tv_ids)} TV shows...")
    for i, tv_id in enumerate(tv_ids, 1):
        if i % 50 == 0: print(f"  Progress: {i}/{len(tv_ids)}")
        data = fetch_comprehensive_tv_data(tv_id)
        if data: all_media.append(data)
    
    print(f"\n✓ Successfully fetched {len(all_media)} items")
    
    # Step 4: Merge with existing (deduplicate)
    print("\n[Step 4/4] Merging and deduplicating...")
    merged_media = {}
    merged_media.update(existing_media)  # Start with existing
    
    # Add/update with new data
    new_count = 0
    updated_count = 0
    for item in all_media:
        media_type = item.get('media_type', 'movie')
        composite_id = f"{media_type}_{item['id']}"
        
        if composite_id in merged_media:
            updated_count += 1
        else:
            new_count += 1
        merged_media[composite_id] = item
    
    print(f"  New items: {new_count}")
    print(f"  Updated items: {updated_count}")
    print(f"  Total items: {len(merged_media)}")
    
    # Save to file
    os.makedirs("data", exist_ok=True)
    merged_list = list(merged_media.values())
    with open(output_file, 'w', encoding='utf-8') as f:
        json.dump(merged_list, f, ensure_ascii=False, indent=2)
    
    print(f"\n✓ Saved {len(merged_list)} items to {output_file}")
    print("Next step: Run generate_embeddings.py to upload to Chroma Cloud")
    
    return merged_list


if __name__ == "__main__":
    collect_media(
        years_back=3,
        max_pages_per_year=50,  # Maximum coverage: 50 pages × 20 items = ~1000 per year (~8000 total)
        output_file="data/movies.json"
    )
