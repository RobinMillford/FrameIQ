"""
Analytics API Routes
Handles data aggregation for user statistics and charts
"""
from flask import Blueprint, jsonify
from flask_login import login_required
from models import db, User, Review, MediaItem, user_watchlist
from sqlalchemy import func
from collections import Counter
from datetime import datetime, timedelta

analytics = Blueprint('analytics', __name__)

@analytics.route('/api/users/<int:user_id>/stats', methods=['GET'])
@login_required
def get_user_stats(user_id):
    """Get aggregated statistics for a specific user.

    Watch-count semantics (movies watched / TV watched / total
    watched) come from the CANONICAL statistics service — the same
    source the profile header consumes — so the two surfaces can never
    diverge again (previously this endpoint counted ``user_viewed``
    rows, a table TV watches never enter, which reported 0 TV for
    any episode-watching user).
    """
    from api.statistics import get_statistics

    user = User.query.get_or_404(user_id)

    # 1. basic counts — watch metrics from the canonical service
    canonical = get_statistics(user_id, lifetime=True)
    total_watched = (canonical["movies_watched"]
                     + canonical["tv_shows_watched"])
    movie_watched = canonical["movies_watched"]
    tv_watched = canonical["tv_shows_watched"]

    total_reviews = Review.query.filter_by(user_id=user_id, is_deleted=False).count()

    # Watchlist Completion
    total_watchlist = db.session.query(user_watchlist).filter(user_watchlist.c.user_id == user_id).count()
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
    
    # 4. Monthly Activity (last 6 months). Grouped in Python over a
    # bounded scalar projection so the query stays portable across
    # Postgres (prod) and SQLite (tests) — date_trunc is PG-only.
    six_months_ago = datetime.utcnow() - timedelta(days=180)
    created_rows = db.session.query(Review.created_at).filter(
        Review.user_id == user_id,
        Review.is_deleted == False,
        Review.created_at >= six_months_ago
    ).all()

    month_counts = Counter()
    for (created,) in created_rows:
        if created:
            month_counts[created.strftime("%Y-%m")] += 1

    months = []
    counts = []
    for key in sorted(month_counts):
        year, mon = key.split("-")
        months.append(datetime(int(year), int(mon), 1).strftime("%b %Y"))
        counts.append(month_counts[key])
    
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
