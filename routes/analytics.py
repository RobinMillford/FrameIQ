"""
Analytics API Routes
Handles data aggregation for user statistics and charts
"""
from flask import Blueprint, jsonify, request
from flask_login import current_user, login_required
from models import db, User, Review, MediaItem, user_viewed
from sqlalchemy import func, extract
from collections import Counter
from datetime import datetime, timedelta

analytics = Blueprint('analytics', __name__)

@analytics.route('/api/users/<int:user_id>/stats', methods=['GET'])
@login_required
def get_user_stats(user_id):
    """Get aggregated statistics for a specific user"""
    user = User.query.get_or_404(user_id)
    
    # 1. basic counts
    total_reviews = Review.query.filter_by(user_id=user_id, is_deleted=False).count()
    
    # Total watched (from user_viewed table)
    total_watched = db.session.query(user_viewed).filter(user_viewed.c.user_id == user_id).count()
    
    # Media Type Breakdown
    movie_watched = db.session.query(user_viewed).filter(
        user_viewed.c.user_id == user_id, 
        user_viewed.c.media_type == 'movie'
    ).count()
    tv_watched = db.session.query(user_viewed).filter(
        user_viewed.c.user_id == user_id, 
        user_viewed.c.media_type == 'tv'
    ).count()

    # Watchlist Completion
    total_watchlist = db.session.query(db.Table('user_watchlist', db.metadata, autoload_with=db.engine)).filter_by(user_id=user_id).count()
    watchlist_ratio = round((total_watched / (total_watched + total_watchlist) * 100), 1) if (total_watched + total_watchlist) > 0 else 0
    
    # 2. Average Rating
    avg_rating_row = db.session.query(func.avg(Review.rating)).filter(
        Review.user_id == user_id, 
        Review.is_deleted == False
    ).first()
    avg_rating = round(float(avg_rating_row[0]), 2) if avg_rating_row[0] else 0
    
    # Rating Distribution (Histogram)
    rating_counts = db.session.query(Review.rating, func.count(Review.id)).filter(
        Review.user_id == user_id, 
        Review.is_deleted == False
    ).group_by(Review.rating).all()
    
    # Initialize buckets for 0.5 to 5.0
    dist_map = {float(i)/2: 0 for i in range(1, 11)}
    for r, count in rating_counts:
        dist_map[float(r)] = count
        
    rating_dist = {
        'labels': [str(k) for k in sorted(dist_map.keys())],
        'data': [dist_map[k] for k in sorted(dist_map.keys())]
    }

    # 3. Genre Distribution & Performance
    # Get all media items reviewed by user
    media_items_query = db.session.query(MediaItem.genres, Review.rating).join(
        Review, MediaItem.id == Review.media_id
    ).filter(Review.user_id == user_id, Review.is_deleted == False).all()
    
    genre_counts = Counter()
    genre_ratings = {} # genre -> [ratings]
    
    for genres, rating in media_items_query:
        if genres:
            genre_names = [g.strip() for g in genres.split(',') if g.strip()]
            for g in genre_names:
                genre_counts[g] += 1
                if g not in genre_ratings:
                    genre_ratings[g] = []
                genre_ratings[g].append(rating)
    
    # Top 5 genres by count
    top_genres = genre_counts.most_common(5)
    genre_data = {
        'labels': [g[0] for g in top_genres],
        'data': [g[1] for g in top_genres]
    }
    
    # Genre performance (Top 5 by average rating, min 2 reviews if possible)
    perf_list = []
    for g, ratings in genre_ratings.items():
        avg = sum(ratings) / len(ratings)
        perf_list.append((g, round(avg, 2), len(ratings)))
    
    # Sort by avg rating, then count
    perf_list.sort(key=lambda x: (x[1], x[2]), reverse=True)
    perf_data = {
        'labels': [p[0] for p in perf_list[:5]],
        'data': [p[1] for p in perf_list[:5]]
    }
    
    # 4. Monthly Activity (last 6 months)
    six_months_ago = datetime.utcnow() - timedelta(days=180)
    activity_query = db.session.query(
        func.date_trunc('month', Review.created_at).label('month'),
        func.count(Review.id).label('count')
    ).filter(
        Review.user_id == user_id,
        Review.is_deleted == False,
        Review.created_at >= six_months_ago
    ).group_by(func.date_trunc('month', Review.created_at)).order_by('month').all()
    
    months = []
    counts = []
    for row in activity_query:
        months.append(row.month.strftime('%b %Y'))
        counts.append(row.count)
    
    activity_data = {
        'labels': months,
        'data': counts
    }
    
    return jsonify({
        'user_id': user_id,
        'username': user.username,
        'stats': {
            'total_reviews': total_reviews,
            'total_watched': total_watched,
            'avg_rating': avg_rating,
            'movies_watched': movie_watched,
            'tv_watched': tv_watched,
            'watchlist_completion': watchlist_ratio
        },
        'genre_distribution': genre_data,
        'genre_performance': perf_data,
        'rating_distribution': rating_dist,
        'monthly_activity': activity_data
    }), 200
