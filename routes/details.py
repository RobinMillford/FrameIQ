from flask import Blueprint, render_template
from flask_login import current_user
from api.tmdb_client import fetch_movie_details, fetch_tv_show_details, fetch_actor_details
from datetime import datetime
from models import UserListItem, DiaryEntry, WatchProgress, Review

details = Blueprint('details', __name__)


def _taste_match(user_id, item_genres):
    """Percentage (0-100) affinity between the user's rating history and
    the item's genres. None when there's no history to judge with."""
    if not item_genres:
        return None
    try:
        item_set = {g.strip().lower() for g in item_genres if g.strip()}
        if not item_set:
            return None
        reviews = (
            Review.query.filter_by(user_id=user_id)
            .order_by(Review.created_at.desc()).limit(40).all()
        )
        if len(reviews) < 2:
            return None
        genre_hits = {}
        for r in reviews:
            if r.media and r.media.genres:
                for g in str(r.media.genres).split(','):
                    g = g.strip().lower()
                    if g:
                        genre_hits[g] = genre_hits.get(g, 0) + 1
        if not genre_hits:
            return None
        overlap = sum(genre_hits.get(g, 0) for g in item_set)
        # 55% floor — anything shares some DNA with your history
        raw = overlap / max(len(item_set), 1)
        return int(55 + min(raw, 1.0) * 44)
    except Exception:
        return None


@details.route('/movie/<int:movie_id>')
def movie_detail(movie_id):
    try:
        movie = fetch_movie_details(movie_id)
        
        # Get user's lists if authenticated
        user_watchlist_ids = set()
        user_wishlist_ids = set()
        user_viewed_ids = set()
        user_lists_with_movie = []
        diary_entries = []
        
        if current_user.is_authenticated:
            user_watchlist_ids = {(item.tmdb_id, item.media_type) for item in current_user.watchlist}
            user_wishlist_ids = {(item.tmdb_id, item.media_type) for item in current_user.wishlist}
            user_viewed_ids = {(item.tmdb_id, item.media_type) for item in current_user.viewed_media}
            
            # Find which lists contain this movie
            list_items = UserListItem.query.filter_by(
                media_id=movie_id,
                media_type='movie'
            ).all()
            
            for item in list_items:
                if item.user_list.user_id == current_user.id:
                    user_lists_with_movie.append(item.user_list)
            
            # Find diary entries for this movie
            diary_entries = DiaryEntry.query.filter_by(
                user_id=current_user.id,
                media_id=movie_id,
                media_type='movie'
            ).order_by(DiaryEntry.watched_date.desc()).all()
        
        taste_match = _taste_match(current_user.id, movie.get('genres')) if current_user.is_authenticated else None

        return render_template('movie_detail.html', movie=movie,
                               taste_match=taste_match,
                               user_watchlist_ids=user_watchlist_ids,
                               user_wishlist_ids=user_wishlist_ids,
                               user_viewed_ids=user_viewed_ids,
                               user_lists_with_movie=user_lists_with_movie,
                               diary_entries=diary_entries,
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
        user_lists_with_show = []
        diary_entries = []
        
        if current_user.is_authenticated:
            user_watchlist_ids = {(item.tmdb_id, item.media_type) for item in current_user.watchlist}
            user_wishlist_ids = {(item.tmdb_id, item.media_type) for item in current_user.wishlist}
            user_viewed_ids = {(item.tmdb_id, item.media_type) for item in current_user.viewed_media}
            
            # Find which lists contain this TV show
            list_items = UserListItem.query.filter_by(
                media_id=show_id,
                media_type='tv'
            ).all()
            
            for item in list_items:
                if item.user_list.user_id == current_user.id:
                    user_lists_with_show.append(item.user_list)
            
            # Find diary entries for this TV show
            diary_entries = DiaryEntry.query.filter_by(
                user_id=current_user.id,
                media_id=show_id,
                media_type='tv'
            ).order_by(DiaryEntry.watched_date.desc()).all()

        # Last-watched episode for Watch Now button (resume logic)
        watch_resume = None
        if current_user.is_authenticated:
            last_wp = (WatchProgress.query
                       .filter(
                           WatchProgress.user_id == current_user.id,
                           WatchProgress.tmdb_id == show_id,
                           WatchProgress.media_type.in_(['tv', 'anime']),
                           WatchProgress.season.isnot(None),
                           WatchProgress.episode.isnot(None),
                       )
                       .order_by(WatchProgress.updated_at.desc())
                       .first())
            if last_wp and last_wp.progress_pct < 90:
                watch_resume = last_wp

        taste_match = _taste_match(current_user.id, show.get('genres')) if current_user.is_authenticated else None

        return render_template('tv_detail.html', show=show,
                               taste_match=taste_match,
                               user_watchlist_ids=user_watchlist_ids,
                               user_wishlist_ids=user_wishlist_ids,
                               user_viewed_ids=user_viewed_ids,
                               user_lists_with_show=user_lists_with_show,
                               diary_entries=diary_entries,
                               watch_resume=watch_resume,
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
