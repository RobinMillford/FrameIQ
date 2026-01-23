"""
Trending System API Routes
Tracks and displays trending media, tags, and users
"""
from flask import Blueprint, request, jsonify
from flask_login import login_required, current_user
from models import db, MediaItem, Tag, User, Review, MediaLike, MediaComment, UserMediaTag
from sqlalchemy import func, desc, and_
from datetime import datetime, timedelta

trending = Blueprint('trending', __name__)


@trending.route('/api/trending/media', methods=['GET'])
def get_trending_media():
    """Get trending movies and TV shows based on recent activity"""
    days = request.args.get('days', 7, type=int)
    media_type = request.args.get('type', None)  # 'movie', 'tv', or None for both
    limit = request.args.get('limit', 20, type=int)
    
    since_date = datetime.utcnow() - timedelta(days=days)
    
    # Score based on multiple factors:
    # - Number of likes
    # - Number of comments
    # - Number of reviews
    # - Number of tags added
    # - Recency bonus
    
    # Query to calculate trending scores
    base_query = db.session.query(
        MediaItem.id,
        MediaItem.tmdb_id,
        MediaItem.media_type,
        MediaItem.title,
        MediaItem.poster_path,
        func.count(MediaLike.id).label('like_count'),
    ).outerjoin(MediaLike, and_(
        MediaLike.media_id == MediaItem.tmdb_id,
        MediaLike.media_type == MediaItem.media_type,
        MediaLike.created_at >= since_date
    ))
    
    if media_type:
        base_query = base_query.filter(MediaItem.media_type == media_type)
    
    base_query = base_query.group_by(
        MediaItem.id, MediaItem.tmdb_id, MediaItem.media_type, 
        MediaItem.title, MediaItem.poster_path
    )
    
    results = base_query.order_by(desc('like_count')).limit(limit).all()
    
    # Enhance with additional data
    trending_items = []
    for item in results:
        # Get comment count
        comment_count = MediaComment.query.filter(
            MediaComment.media_id == item.tmdb_id,
            MediaComment.media_type == item.media_type,
            MediaComment.created_at >= since_date,
            MediaComment.is_deleted == False
        ).count()
        
        # Get review count
        review_count = Review.query.filter(
            Review.media_id == item.tmdb_id,
            Review.media_type == item.media_type,
            Review.created_at >= since_date
        ).count()
        
        # Calculate trending score
        score = (item.like_count * 2) + (comment_count * 3) + (review_count * 5)
        
        trending_items.append({
            'id': item.id,
            'tmdb_id': item.tmdb_id,
            'media_type': item.media_type,
            'title': item.title,
            'poster_path': item.poster_path,
            'score': score,
            'like_count': item.like_count,
            'comment_count': comment_count,
            'review_count': review_count
        })
    
    # Re-sort by calculated score
    trending_items.sort(key=lambda x: x['score'], reverse=True)
    
    return jsonify({
        'success': True,
        'period_days': days,
        'media_type': media_type or 'all',
        'items': trending_items[:limit]
    })


@trending.route('/api/trending/tags', methods=['GET'])
def get_trending_tags():
    """Get trending tags based on recent usage"""
    days = request.args.get('days', 7, type=int)
    limit = request.args.get('limit', 20, type=int)
    
    since_date = datetime.utcnow() - timedelta(days=days)
    
    # Count recent tag usage
    trending_tags = db.session.query(
        Tag.id,
        Tag.name,
        Tag.usage_count,
        func.count(UserMediaTag.id).label('recent_usage')
    ).join(
        UserMediaTag, UserMediaTag.tag_id == Tag.id
    ).filter(
        UserMediaTag.created_at >= since_date
    ).group_by(
        Tag.id, Tag.name, Tag.usage_count
    ).order_by(
        desc('recent_usage')
    ).limit(limit).all()
    
    return jsonify({
        'success': True,
        'period_days': days,
        'tags': [{
            'id': tag.id,
            'name': tag.name,
            'total_usage': tag.usage_count,
            'recent_usage': tag.recent_usage
        } for tag in trending_tags]
    })


@trending.route('/api/trending/users', methods=['GET'])
def get_trending_users():
    """Get most active users based on recent activity"""
    days = request.args.get('days', 7, type=int)
    limit = request.args.get('limit', 20, type=int)
    
    since_date = datetime.utcnow() - timedelta(days=days)
    
    # Count activities: reviews, comments, likes
    user_activity = db.session.query(
        User.id,
        User.username,
        User.profile_picture,
        User.followers_count,
        func.count(Review.id).label('review_count')
    ).outerjoin(
        Review, and_(Review.user_id == User.id, Review.created_at >= since_date)
    ).group_by(
        User.id, User.username, User.profile_picture, User.followers_count
    ).all()
    
    # Add comment counts
    users_with_scores = []
    for user in user_activity:
        comment_count = MediaComment.query.filter(
            MediaComment.user_id == user.id,
            MediaComment.created_at >= since_date,
            MediaComment.is_deleted == False
        ).count()
        
        like_count = MediaLike.query.filter(
            MediaLike.user_id == user.id,
            MediaLike.created_at >= since_date
        ).count()
        
        # Calculate activity score
        score = (user.review_count * 5) + (comment_count * 3) + (like_count * 1)
        
        users_with_scores.append({
            'id': user.id,
            'username': user.username,
            'profile_picture': user.profile_picture,
            'followers_count': user.followers_count or 0,
            'activity_score': score,
            'review_count': user.review_count,
            'comment_count': comment_count,
            'like_count': like_count
        })
    
    # Sort by activity score
    users_with_scores.sort(key=lambda x: x['activity_score'], reverse=True)
    
    return jsonify({
        'success': True,
        'period_days': days,
        'users': users_with_scores[:limit]
    })


@trending.route('/api/trending/reviews', methods=['GET'])
def get_trending_reviews():
    """Get trending/popular reviews based on likes"""
    days = request.args.get('days', 30, type=int)
    limit = request.args.get('limit', 10, type=int)
    
    since_date = datetime.utcnow() - timedelta(days=days)
    
    # Get reviews with like counts
    trending_reviews = db.session.query(
        Review,
        func.count(MediaLike.id).label('like_count')
    ).outerjoin(
        MediaLike, and_(
            MediaLike.media_id == Review.media_id,
            MediaLike.media_type == Review.media_type,
            MediaLike.user_id == Review.user_id
        )
    ).filter(
        Review.created_at >= since_date
    ).group_by(Review.id).order_by(desc('like_count')).limit(limit).all()
    
    reviews_data = []
    for review, like_count in trending_reviews:
        reviews_data.append({
            'id': review.id,
            'user_id': review.user_id,
            'username': review.user.username,
            'media_id': review.media_id,
            'media_type': review.media_type,
            'media_title': review.media_title,
            'rating': review.rating,
            'content': review.content,
            'created_at': review.created_at.isoformat(),
            'like_count': like_count
        })
    
    return jsonify({
        'success': True,
        'period_days': days,
        'reviews': reviews_data
    })


@trending.route('/api/trending/summary', methods=['GET'])
def get_trending_summary():
    """Get a summary of all trending data"""
    days = request.args.get('days', 7, type=int)
    
    # Get top 5 of each category
    trending_media_resp = get_trending_media()
    trending_tags_resp = get_trending_tags()
    trending_users_resp = get_trending_users()
    
    media_data = trending_media_resp.get_json()
    tags_data = trending_tags_resp.get_json()
    users_data = trending_users_resp.get_json()
    
    return jsonify({
        'success': True,
        'period_days': days,
        'summary': {
            'top_media': media_data['items'][:5],
            'top_tags': tags_data['tags'][:5],
            'top_users': users_data['users'][:5]
        }
    })
