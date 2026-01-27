from flask import Blueprint, render_template, request, jsonify, redirect, url_for, flash
import requests
import time
import os
from datetime import datetime
from sqlalchemy import select
from api.tmdb_client import (
    fetch_now_playing_movies, fetch_popular_movies, fetch_upcoming_movies,
    fetch_trending_people, fetch_airing_today_shows, fetch_on_the_air_shows,
    fetch_popular_shows, fetch_trending_movies, fetch_movies_by_genre,
    fetch_shows_by_genre, fetch_poster, fetch_tmdb_recommendations
)
from flask_login import login_required, current_user
from models import db, MediaItem, user_watchlist, user_wishlist, user_viewed, User, UserFollow, Review

# Get environment variables
TMDB_API_KEY = os.getenv("TMDB_API_KEY")
TMDB_API_KEY_2 = os.getenv("TMDB_API_KEY_2")
NEWS_API_KEY = os.getenv("NEWS_API_KEY")

main = Blueprint('main', __name__)

@main.route('/')
def index():
    trending_backdrops = fetch_trending_movies()
    now_playing = fetch_now_playing_movies()
    popular = fetch_popular_movies()
    # Get IDs from now_playing and popular to exclude
    exclude_ids = {movie['id'] for movie in now_playing + popular}
    upcoming = fetch_upcoming_movies(exclude_ids=exclude_ids)
    airing_today = fetch_airing_today_shows()
    on_the_air = fetch_on_the_air_shows()
    popular_shows = fetch_popular_shows()
    trending_people = fetch_trending_people()
    
    # Get user's lists if authenticated
    user_watchlist_ids = set()
    user_wishlist_ids = set()
    user_viewed_ids = set()
    
    if current_user.is_authenticated:
        user_watchlist_ids = {(item.tmdb_id, item.media_type) for item in current_user.watchlist}
        user_wishlist_ids = {(item.tmdb_id, item.media_type) for item in current_user.wishlist}
        user_viewed_ids = {(item.tmdb_id, item.media_type) for item in current_user.viewed_media}
    
    return render_template(
        'index.html',
        trending_backdrops=trending_backdrops,
        now_playing=now_playing,
        popular=popular,
        upcoming=upcoming,
        airing_today=airing_today,
        on_the_air=on_the_air,
        popular_shows=popular_shows,
        trending_people=trending_people,
        user_watchlist_ids=user_watchlist_ids,
        user_wishlist_ids=user_wishlist_ids,
        user_viewed_ids=user_viewed_ids
    )

@main.route('/search', methods=['GET', 'POST'])
def search():
    # Support both form data (POST) and query parameters (GET)
    query = request.form.get('query') or request.args.get('query') or request.args.get('q')
    if not query:
        return render_template('index.html', error="Please enter a search term.")

    # Search movies
    movie_url = f"https://api.themoviedb.org/3/search/movie?api_key={TMDB_API_KEY}&language=en-US&query={query}&page=1"
    movie_response = requests.get(movie_url)
    movie_data = movie_response.json().get('results', [])

    # Search TV shows
    tv_url = f"https://api.themoviedb.org/3/search/tv?api_key={TMDB_API_KEY}&language=en-US&query={query}&page=1"
    tv_response = requests.get(tv_url)
    tv_data = tv_response.json().get('results', [])

    # Search people (TMDB)
    person_url = f"https://api.themoviedb.org/3/search/person?api_key={TMDB_API_KEY}&language=en-US&query={query}&page=1"
    person_response = requests.get(person_url)
    person_data = person_response.json().get('results', [])

    # Search community members (Local DB)
    community_members = User.query.filter(User.username.ilike(f'%{query}%')).all()

    # Format all results, filtering out items without images
    movies = [
        {
            'id': m['id'],
            'title': m['title'],
            'release_date': m.get('release_date', 'N/A'),
            'poster_path': m.get('poster_path')
        } for m in movie_data if m.get('poster_path')
    ]
    shows = [
        {
            'id': s['id'],
            'name': s['name'],
            'first_air_date': s.get('first_air_date', 'N/A'),
            'poster_path': s.get('poster_path')
        } for s in tv_data if s.get('poster_path')
    ]
    people = [
        {
            'id': p['id'],
            'name': p['name'],
            'known_for': p.get('known_for_department', 'N/A'),
            'profile_path': p.get('profile_path')
        } for p in person_data if p.get('profile_path')
    ]
    
    # Get user's lists if authenticated
    user_watchlist_ids = set()
    user_wishlist_ids = set()
    user_viewed_ids = set()
    following_ids = set()
    
    if current_user.is_authenticated:
        user_watchlist_ids = {(item.tmdb_id, item.media_type) for item in current_user.watchlist}
        user_wishlist_ids = {(item.tmdb_id, item.media_type) for item in current_user.wishlist}
        user_viewed_ids = {(item.tmdb_id, item.media_type) for item in current_user.viewed_media}
        following_ids = {f.following_id for f in current_user.following if f.is_active}

    return render_template('search_results.html', query=query, movies=movies, shows=shows, people=people,
                          community_members=community_members,
                          user_watchlist_ids=user_watchlist_ids, user_wishlist_ids=user_wishlist_ids, user_viewed_ids=user_viewed_ids,
                          following_ids=following_ids)

@main.route('/autocomplete')
def autocomplete():
    query = request.args.get('query', '')
    if not query:
        return jsonify({'movies': [], 'shows': [], 'people': []})

    # Search movies
    movie_url = f"https://api.themoviedb.org/3/search/movie?api_key={TMDB_API_KEY}&language=en-US&query={query}&page=1"
    movie_response = requests.get(movie_url)
    movie_data = movie_response.json().get('results', [])

    # Search TV shows
    tv_url = f"https://api.themoviedb.org/3/search/tv?api_key={TMDB_API_KEY}&language=en-US&query={query}&page=1"
    tv_response = requests.get(tv_url)
    tv_data = tv_response.json().get('results', [])

    # Search people
    person_url = f"https://api.themoviedb.org/3/search/person?api_key={TMDB_API_KEY}&language=en-US&query={query}&page=1"
    person_response = requests.get(person_url)
    person_data = person_response.json().get('results', [])

    # Format results, filtering out items without images (limit 5 each)
    movies = [{'id': m['id'], 'title': m['title'], 'release_date': m.get('release_date'), 'poster_path': m.get('poster_path')} 
              for m in movie_data if m.get('poster_path')][:5]
    shows = [{'id': s['id'], 'name': s['name'], 'first_air_date': s.get('first_air_date'), 'poster_path': s.get('poster_path')} 
             for s in tv_data if s.get('poster_path')][:5]
    people = [{'id': p['id'], 'name': p['name'], 'known_for': p.get('known_for_department'), 'profile_path': p.get('profile_path')} 
              for p in person_data if p.get('profile_path')][:5]

    return jsonify({'movies': movies, 'shows': shows, 'people': people})

@main.route('/trending')
def trending_page():
    """Render the trending page"""
    return render_template('trending.html')

@main.route('/news')
def news():
    # NewsAPI call with refined query
    news_url = (
        f"https://newsapi.org/v2/everything?"
        f"q=movies OR 'TV shows' OR 'movie actors' OR 'TV actors' OR actresses "
        f"-sports -politics -business -tech "
        f"&apiKey={NEWS_API_KEY}&language=en&sortBy=publishedAt&pageSize=50"
    )
    news_response = requests.get(news_url)
    news_data = news_response.json()
    articles = news_data.get('articles', [])
    filtered_articles = [
        {
            'title': article['title'],
            'description': article['description'] or 'No description available',
            'url': article['url'],
            'urlToImage': article['urlToImage'],
            'publishedAt': article['publishedAt']
        } for article in articles 
        if article.get('title') and article.get('url') and article.get('urlToImage') and  # Require image
           any(keyword in article['title'].lower() or keyword in (article['description'] or '').lower() 
               for keyword in ['movie', 'tv', 'actor', 'actress', 'show', 'film', 'series', 'cinema', 
                               'television', 'star', 'celebrity', 'director', 'producer', 'screenplay', 
                               'premiere', 'release', 'cast', 'episode', 'season'])
    ]
    return render_template('news.html', articles=filtered_articles)

@main.route('/movies')
def movies():
    return render_template('movies.html', api_key=TMDB_API_KEY_2)

@main.route('/tv_shows')
def tv_shows():
    return render_template('tv_shows.html', api_key=TMDB_API_KEY_2)

@main.route('/genre/<genre_name>')
def genre_page(genre_name):
    genre_mapping = {
        'romance': 10749, 'horror': 27, 'fantasy': 14, 'science_fiction': 878, 'mystery': 9648,
        'western': 37, 'drama': 18, 'action': 28, 'comedy': 35, 'thriller': 53, 'adventure': 12,
        'animation': 16, 'crime': 80, 'family': 10751, 'history': 36, 'music': 10402, 'war': 10752,
        'documentary': 99, 'tv_movie': 10770
    }
    genre_id = genre_mapping.get(genre_name)
    if genre_id:
        movies = fetch_movies_by_genre(genre_id)
        
        # Get user's lists if authenticated
        user_watchlist_ids = set()
        user_wishlist_ids = set()
        user_viewed_ids = set()
        
        if current_user.is_authenticated:
            user_watchlist_ids = {(item.tmdb_id, item.media_type) for item in current_user.watchlist}
            user_wishlist_ids = {(item.tmdb_id, item.media_type) for item in current_user.wishlist}
            user_viewed_ids = {(item.tmdb_id, item.media_type) for item in current_user.viewed_media}
        
        return render_template('genre.html', genre_name=genre_name.capitalize(), movies=movies,
                              user_watchlist_ids=user_watchlist_ids,
                              user_wishlist_ids=user_wishlist_ids,
                              user_viewed_ids=user_viewed_ids)
    else:
        return render_template('genre_not_found.html', genre_name=genre_name.capitalize())

@main.route('/tv_genre/<genre_name>')
def tv_genre_page(genre_name):
    genre_mapping = {
        'action_adventure': 10759, 'animation': 16, 'comedy': 35, 'crime': 80, 'documentary': 99,
        'drama': 18, 'family': 10751, 'kids': 10762, 'mystery': 9648, 'news': 10763, 'reality': 10764,
        'sci_fi_fantasy': 10765, 'soap': 10766, 'talk': 10767, 'war_politics': 10768, 'western': 37
    }
    genre_id = genre_mapping.get(genre_name)
    if genre_id:
        shows = fetch_shows_by_genre(genre_id)
        
        # Get user's lists if authenticated
        user_watchlist_ids = set()
        user_wishlist_ids = set()
        user_viewed_ids = set()
        
        if current_user.is_authenticated:
            user_watchlist_ids = {(item.tmdb_id, item.media_type) for item in current_user.watchlist}
            user_wishlist_ids = {(item.tmdb_id, item.media_type) for item in current_user.wishlist}
            user_viewed_ids = {(item.tmdb_id, item.media_type) for item in current_user.viewed_media}
        
        return render_template('tv_genre.html', genre_name=genre_name.replace('_', ' ').capitalize(), shows=shows,
                              user_watchlist_ids=user_watchlist_ids,
                              user_wishlist_ids=user_wishlist_ids,
                              user_viewed_ids=user_viewed_ids)
    else:
        return render_template('genre_not_found.html', genre_name=genre_name.replace('_', ' ').capitalize())

@main.route('/recommend', methods=['POST'])
def recommend():
    movie_name = request.form['movie_name']
    tmdb_search_url = f"https://api.themoviedb.org/3/search/movie?api_key={TMDB_API_KEY}&language=en-US&query={movie_name}&page=1&include_adult=true"
    time.sleep(1)
    response = requests.get(tmdb_search_url)
    data = response.json()
    tmdb_results = data.get('results', [])
    
    if tmdb_results:
        searched_movie = tmdb_results[0]
        searched_movie_id = searched_movie['id']
        searched_movie_poster = fetch_poster(searched_movie_id)
        
        tmdb_recommendations = fetch_tmdb_recommendations(searched_movie_id)
        
        recommend_movie = []
        recommend_poster = []
        recommend_ids = []  # New list to store movie IDs
        
        for rec in tmdb_recommendations:
            if rec['id'] != searched_movie_id:
                recommend_movie.append(rec['title'])
                recommend_poster.append(fetch_poster(rec['id']))
                recommend_ids.append(rec['id'])  # Store the movie ID

        # Get user's lists if authenticated
        user_watchlist_ids = set()
        user_wishlist_ids = set()
        user_viewed_ids = set()
        
        if current_user.is_authenticated:
            user_watchlist_ids = {(item.tmdb_id, item.media_type) for item in current_user.watchlist}
            user_wishlist_ids = {(item.tmdb_id, item.media_type) for item in current_user.wishlist}
            user_viewed_ids = {(item.tmdb_id, item.media_type) for item in current_user.viewed_media}

        return render_template('recommend.html', 
                               searched_movie=movie_name, 
                               searched_movie_poster=searched_movie_poster,
                               recommend_movie=recommend_movie, 
                               recommend_poster=recommend_poster, 
                               recommend_ids=recommend_ids,
                               user_watchlist_ids=user_watchlist_ids,
                               user_wishlist_ids=user_wishlist_ids,
                               user_viewed_ids=user_viewed_ids)  # Pass the IDs to the template
    else:
        return render_template('no_results.html', searched_movie=movie_name)

@main.route('/tv_recommend', methods=['POST'])
def tv_recommend():
    show_name = request.form['show_name']
    tmdb_search_url = f"https://api.themoviedb.org/3/search/tv?api_key={TMDB_API_KEY}&language=en-US&query={show_name}&page=1&include_adult=true"
    time.sleep(1)
    response = requests.get(tmdb_search_url)
    data = response.json()
    tmdb_results = data.get('results', [])
    
    if tmdb_results:
        searched_show = tmdb_results[0]
        searched_show_id = searched_show['id']
        searched_show_poster = fetch_poster(searched_show_id, is_movie=False)
        
        tmdb_recommendations = fetch_tmdb_recommendations(searched_show_id, is_movie=False)
        
        recommend_show = []
        recommend_poster = []
        recommend_ids = []  # New list to store TV show IDs
        
        for rec in tmdb_recommendations:
            if rec['id'] != searched_show_id:
                recommend_show.append(rec['name'])
                recommend_poster.append(fetch_poster(rec['id'], is_movie=False))
                recommend_ids.append(rec['id'])  # Store the TV show ID

        # Get user's lists if authenticated
        user_watchlist_ids = set()
        user_wishlist_ids = set()
        user_viewed_ids = set()
        
        if current_user.is_authenticated:
            user_watchlist_ids = {(item.tmdb_id, item.media_type) for item in current_user.watchlist}
            user_wishlist_ids = {(item.tmdb_id, item.media_type) for item in current_user.wishlist}
            user_viewed_ids = {(item.tmdb_id, item.media_type) for item in current_user.viewed_media}

        return render_template('tv_recommend.html', 
                               searched_show=show_name, 
                               searched_show_poster=searched_show_poster,
                               recommend_show=recommend_show, 
                               recommend_poster=recommend_poster, 
                               recommend_ids=recommend_ids,
                               user_watchlist_ids=user_watchlist_ids,
                               user_wishlist_ids=user_wishlist_ids,
                               user_viewed_ids=user_viewed_ids)  # Pass the IDs to the template
    else:
        return render_template('no_results.html', searched_show=show_name)

@main.route('/watchlist')
@login_required
def watchlist():
    """Display user's watchlist"""
    watchlist_items = current_user.watchlist
    
    # Get user's lists for status indicators
    user_watchlist_ids = {(item.tmdb_id, item.media_type) for item in current_user.watchlist}
    user_wishlist_ids = {(item.tmdb_id, item.media_type) for item in current_user.wishlist}
    user_viewed_ids = {(item.tmdb_id, item.media_type) for item in current_user.viewed_media}
    
    return render_template('watchlist.html', watchlist=watchlist_items,
                          user_watchlist_ids=user_watchlist_ids,
                          user_wishlist_ids=user_wishlist_ids,
                          user_viewed_ids=user_viewed_ids)

@main.route('/wishlist')
@login_required
def wishlist():
    """Display user's wishlist"""
    wishlist_items = current_user.wishlist
    
    # Get user's lists for status indicators
    user_watchlist_ids = {(item.tmdb_id, item.media_type) for item in current_user.watchlist}
    user_wishlist_ids = {(item.tmdb_id, item.media_type) for item in current_user.wishlist}
    user_viewed_ids = {(item.tmdb_id, item.media_type) for item in current_user.viewed_media}
    
    return render_template('wishlist.html', wishlist=wishlist_items,
                          user_watchlist_ids=user_watchlist_ids,
                          user_wishlist_ids=user_wishlist_ids,
                          user_viewed_ids=user_viewed_ids)

@main.route('/viewed')
@login_required
def viewed():
    """Display user's viewing history"""
    viewed_items = current_user.viewed_media
    
    # Get user's lists for status indicators
    user_watchlist_ids = {(item.tmdb_id, item.media_type) for item in current_user.watchlist}
    user_wishlist_ids = {(item.tmdb_id, item.media_type) for item in current_user.wishlist}
    user_viewed_ids = {(item.tmdb_id, item.media_type) for item in current_user.viewed_media}
    
    return render_template('viewed.html', viewed=viewed_items,
                          user_watchlist_ids=user_watchlist_ids,
                          user_wishlist_ids=user_wishlist_ids,
                          user_viewed_ids=user_viewed_ids)

@main.route('/add_to_watchlist/<int:media_id>/<media_type>', methods=['GET'])
@login_required
def add_to_watchlist(media_id, media_type):
    """Add a movie or TV show to the user's watchlist"""
    # Get priority from query parameter (default: medium)
    priority = request.args.get('priority', 'medium')
    if priority not in ['high', 'medium', 'low']:
        priority = 'medium'
    
    # Check if media item exists in our database, if not create it
    media_item = MediaItem.query.filter_by(tmdb_id=media_id, media_type=media_type).first()
    if not media_item:
        # Fetch from TMDB API
        url = f"https://api.themoviedb.org/3/{media_type}/{media_id}?api_key={TMDB_API_KEY}"
        response = requests.get(url)
        if response.status_code == 200:
            data = response.json()
            title = data.get('title') if media_type == 'movie' else data.get('name')
            release_date_str = data.get('release_date') if media_type == 'movie' else data.get('first_air_date')
            # Convert string date to Python date object
            release_date = None
            if release_date_str:
                try:
                    release_date = datetime.strptime(release_date_str, '%Y-%m-%d').date()
                except ValueError:
                    # Handle invalid date formats
                    release_date = None
            poster_path = data.get('poster_path')
            overview = data.get('overview')
            rating = data.get('vote_average')
            
            media_item = MediaItem(
                tmdb_id=media_id,
                media_type=media_type,
                title=title,
                release_date=release_date,
                poster_path=poster_path,
                overview=overview,
                rating=rating
            )
            db.session.add(media_item)
            db.session.commit()
    
    # Check if already in watchlist
    stmt = db.select(user_watchlist).where(
        user_watchlist.c.user_id == current_user.id,
        user_watchlist.c.media_id == media_item.id,
        user_watchlist.c.media_type == media_type
    )
    result = db.session.execute(stmt).fetchone()
    
    # Add to watchlist if not already there
    if not result:
        stmt = user_watchlist.insert().values(
            user_id=current_user.id,
            media_id=media_item.id,
            media_type=media_type,
            priority=priority
        )
        db.session.execute(stmt)
        db.session.commit()
        priority_label = {'high': '🔥 High', 'medium': '📌 Medium', 'low': '💤 Low'}.get(priority, '')
        flash(f'Added {media_item.title} to your watchlist with {priority_label} priority!')
    else:
        flash(f'{media_item.title} is already in your watchlist!')
    
    # Redirect back to the previous page or to the media detail page
    return redirect(request.referrer or url_for('main.index'))

@main.route('/add_to_wishlist/<int:media_id>/<media_type>', methods=['GET'])
@login_required
def add_to_wishlist(media_id, media_type):
    """Add a movie or TV show to the user's wishlist"""
    # Get priority from query parameter (default: medium)
    priority = request.args.get('priority', 'medium')
    if priority not in ['high', 'medium', 'low']:
        priority = 'medium'
    
    # Check if media item exists in our database, if not create it
    media_item = MediaItem.query.filter_by(tmdb_id=media_id, media_type=media_type).first()
    if not media_item:
        # Fetch from TMDB API
        url = f"https://api.themoviedb.org/3/{media_type}/{media_id}?api_key={TMDB_API_KEY}"
        response = requests.get(url)
        if response.status_code == 200:
            data = response.json()
            title = data.get('title') if media_type == 'movie' else data.get('name')
            release_date_str = data.get('release_date') if media_type == 'movie' else data.get('first_air_date')
            # Convert string date to Python date object
            release_date = None
            if release_date_str:
                try:
                    release_date = datetime.strptime(release_date_str, '%Y-%m-%d').date()
                except ValueError:
                    # Handle invalid date formats
                    release_date = None
            poster_path = data.get('poster_path')
            overview = data.get('overview')
            rating = data.get('vote_average')
            
            media_item = MediaItem(
                tmdb_id=media_id,
                media_type=media_type,
                title=title,
                release_date=release_date,
                poster_path=poster_path,
                overview=overview,
                rating=rating
            )
            db.session.add(media_item)
            db.session.commit()
    
    # Check if already in wishlist
    stmt = db.select(user_wishlist).where(
        user_wishlist.c.user_id == current_user.id,
        user_wishlist.c.media_id == media_item.id,
        user_wishlist.c.media_type == media_type
    )
    result = db.session.execute(stmt).fetchone()
    
    # Add to wishlist if not already there
    if not result:
        stmt = user_wishlist.insert().values(
            user_id=current_user.id,
            media_id=media_item.id,
            media_type=media_type,
            priority=priority
        )
        db.session.execute(stmt)
        db.session.commit()
        priority_label = {'high': '🔥 High', 'medium': '📌 Medium', 'low': '💤 Low'}.get(priority, '')
        flash(f'Added {media_item.title} to your wishlist with {priority_label} priority!')
    else:
        flash(f'{media_item.title} is already in your wishlist!')
    
    # Redirect back to the previous page or to the media detail page
    return redirect(request.referrer or url_for('main.index'))

@main.route('/mark_as_viewed/<int:media_id>/<media_type>', methods=['GET'])
@login_required
def mark_as_viewed(media_id, media_type):
    """Mark a movie or TV show as viewed"""
    # Check if media item exists in our database, if not create it
    media_item = MediaItem.query.filter_by(tmdb_id=media_id, media_type=media_type).first()
    if not media_item:
        # Fetch from TMDB API
        url = f"https://api.themoviedb.org/3/{media_type}/{media_id}?api_key={TMDB_API_KEY}"
        response = requests.get(url)
        if response.status_code == 200:
            data = response.json()
            title = data.get('title') if media_type == 'movie' else data.get('name')
            release_date_str = data.get('release_date') if media_type == 'movie' else data.get('first_air_date')
            # Convert string date to Python date object
            release_date = None
            if release_date_str:
                try:
                    release_date = datetime.strptime(release_date_str, '%Y-%m-%d').date()
                except ValueError:
                    # Handle invalid date formats
                    release_date = None
            poster_path = data.get('poster_path')
            overview = data.get('overview')
            rating = data.get('vote_average')
            
            media_item = MediaItem(
                tmdb_id=media_id,
                media_type=media_type,
                title=title,
                release_date=release_date,
                poster_path=poster_path,
                overview=overview,
                rating=rating
            )
            db.session.add(media_item)
            db.session.commit()
    
    # Check if already viewed
    stmt = db.select(user_viewed).where(
        user_viewed.c.user_id == current_user.id,
        user_viewed.c.media_id == media_item.id,
        user_viewed.c.media_type == media_type
    )
    result = db.session.execute(stmt).fetchone()
    
    # Add to viewed if not already there
    if not result:
        stmt = user_viewed.insert().values(
            user_id=current_user.id,
            media_id=media_item.id,
            media_type=media_type
        )
        db.session.execute(stmt)
        db.session.commit()
        flash(f'Marked {media_item.title} as viewed!')
    else:
        flash(f'{media_item.title} is already marked as viewed!')
    
    # Redirect back to the previous page or to the media detail page
    return redirect(request.referrer or url_for('main.index'))

@main.route('/remove_from_watchlist/<int:media_id>/<media_type>', methods=['GET'])
@login_required
def remove_from_watchlist(media_id, media_type):
    """Remove a movie or TV show from the user's watchlist"""
    media_item = MediaItem.query.filter_by(tmdb_id=media_id, media_type=media_type).first()
    if media_item:
        # Check if in watchlist
        stmt = db.select(user_watchlist).where(
            user_watchlist.c.user_id == current_user.id,
            user_watchlist.c.media_id == media_item.id,
            user_watchlist.c.media_type == media_type
        )
        result = db.session.execute(stmt).fetchone()
        
        if result:
            # Remove from watchlist
            stmt = user_watchlist.delete().where(
                user_watchlist.c.user_id == current_user.id,
                user_watchlist.c.media_id == media_item.id,
                user_watchlist.c.media_type == media_type
            )
            db.session.execute(stmt)
            db.session.commit()
            flash(f'Removed {media_item.title} from your watchlist!')
        else:
            flash('Item not found in your watchlist!')
    else:
        flash('Item not found!')
    
    return redirect(request.referrer or url_for('main.watchlist'))

@main.route('/remove_from_wishlist/<int:media_id>/<media_type>', methods=['GET'])
@login_required
def remove_from_wishlist(media_id, media_type):
    """Remove a movie or TV show from the user's wishlist"""
    media_item = MediaItem.query.filter_by(tmdb_id=media_id, media_type=media_type).first()
    if media_item:
        # Check if in wishlist
        stmt = db.select(user_wishlist).where(
            user_wishlist.c.user_id == current_user.id,
            user_wishlist.c.media_id == media_item.id,
            user_wishlist.c.media_type == media_type
        )
        result = db.session.execute(stmt).fetchone()
        
        if result:
            # Remove from wishlist
            stmt = user_wishlist.delete().where(
                user_wishlist.c.user_id == current_user.id,
                user_wishlist.c.media_id == media_item.id,
                user_wishlist.c.media_type == media_type
            )
            db.session.execute(stmt)
            db.session.commit()
            flash(f'Removed {media_item.title} from your wishlist!')
        else:
            flash('Item not found in your wishlist!')
    else:
        flash('Item not found!')
    
    return redirect(request.referrer or url_for('main.wishlist'))

@main.route('/remove_from_viewed/<int:media_id>/<media_type>', methods=['GET'])
@login_required
def remove_from_viewed(media_id, media_type):
    """Remove a movie or TV show from the user's viewing history"""
    media_item = MediaItem.query.filter_by(tmdb_id=media_id, media_type=media_type).first()
    if media_item:
        # Check if in viewed
        stmt = db.select(user_viewed).where(
            user_viewed.c.user_id == current_user.id,
            user_viewed.c.media_id == media_item.id,
            user_viewed.c.media_type == media_type
        )
        result = db.session.execute(stmt).fetchone()
        
        if result:
            # Remove from viewed
            stmt = user_viewed.delete().where(
                user_viewed.c.user_id == current_user.id,
                user_viewed.c.media_id == media_item.id,
                user_viewed.c.media_type == media_type
            )
            db.session.execute(stmt)
            db.session.commit()
            flash(f'Removed {media_item.title} from your viewing history!')
        else:
            flash('Item not found in your viewing history!')
    else:
        flash('Item not found!')
    
    return redirect(request.referrer or url_for('main.viewed'))
@main.route('/user/<int:user_id>')
@login_required
def user_profile(user_id):
    """View another user's public profile"""
    user = User.query.get_or_404(user_id)
    
    # Check if current user follows this user
    is_following = False
    if current_user.is_authenticated:
        follow = UserFollow.query.filter_by(
            follower_id=current_user.id,
            following_id=user_id,
            is_active=True
        ).first()
        is_following = follow is not None
    
    # Get user reviews
    recent_reviews = user.user_reviews.order_by(db.desc(Review.created_at)).limit(5).all()
    
    return render_template('user_profile.html', 
                         user=user, 
                         is_following=is_following,
                         reviews=recent_reviews)


@main.route('/stats')
@login_required
def stats_dashboard():
    """View personal statistics dashboard"""
    return render_template('stats_dashboard.html')


@main.route('/stats/year-in-review')
@main.route('/stats/year-in-review/<int:year>')
@login_required
def year_in_review(year=None):
    """View Year in Review for a specific year"""
    if year is None:
        year = datetime.now().year
    return render_template('year_in_review.html', year=year)
