from flask import Blueprint, render_template
from flask_login import current_user
from api.tmdb_client import fetch_movie_details, fetch_tv_show_details, fetch_actor_details
from datetime import datetime

details = Blueprint('details', __name__)

@details.route('/movie/<int:movie_id>')
def movie_detail(movie_id):
    try:
        movie = fetch_movie_details(movie_id)
        
        # Get user's lists if authenticated
        user_watchlist_ids = set()
        user_wishlist_ids = set()
        user_viewed_ids = set()
        
        if current_user.is_authenticated:
            user_watchlist_ids = {(item.tmdb_id, item.media_type) for item in current_user.watchlist}
            user_wishlist_ids = {(item.tmdb_id, item.media_type) for item in current_user.wishlist}
            user_viewed_ids = {(item.tmdb_id, item.media_type) for item in current_user.viewed_media}
        
        return render_template('movie_detail.html', movie=movie,
                              user_watchlist_ids=user_watchlist_ids,
                              user_wishlist_ids=user_wishlist_ids,
                              user_viewed_ids=user_viewed_ids,
                              today=datetime.now().strftime('%Y-%m-%d'))
    except Exception as e:
        print(f"Error fetching movie details: {e}")
        return render_template('error.html', message="Could not fetch movie details. Please try again later.")

@details.route('/tv/<int:show_id>')
def tv_detail(show_id):
    try:
        show = fetch_tv_show_details(show_id)
        
        # Get user's lists if authenticated
        user_watchlist_ids = set()
        user_wishlist_ids = set()
        user_viewed_ids = set()
        
        if current_user.is_authenticated:
            user_watchlist_ids = {(item.tmdb_id, item.media_type) for item in current_user.watchlist}
            user_wishlist_ids = {(item.tmdb_id, item.media_type) for item in current_user.wishlist}
            user_viewed_ids = {(item.tmdb_id, item.media_type) for item in current_user.viewed_media}
        
        return render_template('tv_detail.html', show=show,
                              user_watchlist_ids=user_watchlist_ids,
                              user_wishlist_ids=user_wishlist_ids,
                              user_viewed_ids=user_viewed_ids,
                              today=datetime.now().strftime('%Y-%m-%d'))
    except Exception as e:
        print(f"Error fetching TV show details: {e}")
        return render_template('error.html', message="Could not fetch TV show details. Please try again later.")

@details.route('/actor/<int:actor_id>')
def actor_detail(actor_id):
    try:
        actor = fetch_actor_details(actor_id)
        return render_template('actor_detail.html', actor=actor)
    except Exception as e:
        print(f"Error fetching actor details: {e}")
        return render_template('error.html', message="Could not fetch actor details. Please try again later.")