"""
Enhanced Activity Feed Routes
Provides advanced filtering, sorting, and activity type selection
"""
from flask import Blueprint, request, jsonify, render_template
from flask_login import login_required, current_user
from models import db, User, Review, MediaLike, MediaComment, UserFollow, UserMediaTag
from sqlalchemy import or_, and_, desc
from datetime import datetime, timedelta

activity_feed = Blueprint('activity_feed', __name__)


@activity_feed.route('/feed/enhanced')
@login_required
def enhanced_feed_page():
    """Render the enhanced activity feed page"""
    return render_template('feed_enhanced.html')


@activity_feed.route('/api/feed/enhanced', methods=['GET'])
@login_required
def get_enhanced_feed():
    """
    Get enhanced activity feed with filters
    
    Query Parameters:
    - feed_type: 'following' (default) or 'global' or 'personal'
    - activity_types: comma-separated (e.g., 'reviews,likes,comments')
    - page: page number (default: 1)
    - per_page: items per page (default: 20)
    - sort: 'recent' (default) or 'popular'
    - time_range: 'day', 'week', 'month', 'all' (default)
    """
    feed_type = request.args.get('feed_type', 'following')
    activity_types = request.args.get('activity_types', 'reviews,likes,comments,tags').split(',')
    page = request.args.get('page', 1, type=int)
    per_page = request.args.get('per_page', 20, type=int)
    sort_by = request.args.get('sort', 'recent')
    time_range = request.args.get('time_range', 'all')
    
    # Calculate time filter
    since_date = None
    if time_range == 'day':
        since_date = datetime.utcnow() - timedelta(days=1)
    elif time_range == 'week':
        since_date = datetime.utcnow() - timedelta(days=7)
    elif time_range == 'month':
        since_date = datetime.utcnow() - timedelta(days=30)
    
    # Collect activities
    activities = []
    
    # Determine which users to include
    if feed_type == 'following':
        following_ids = [f.following_id for f in current_user.following if f.is_active]
        if not following_ids:
            return jsonify({
                'success': True,
                'activities': [],
                'total': 0,
                'pages': 0,
                'current_page': page
            })
        user_filter = lambda query, user_field: query.filter(user_field.in_(following_ids))
    elif feed_type == 'personal':
        user_filter = lambda query, user_field: query.filter(user_field == current_user.id)
    else:  # global
        user_filter = lambda query, user_field: query
    
    # Fetch Reviews
    if 'reviews' in activity_types:
        reviews_query = Review.query.filter_by(is_deleted=False)
        reviews_query = user_filter(reviews_query, Review.user_id)
        if since_date:
            reviews_query = reviews_query.filter(Review.created_at >= since_date)
        
        reviews = reviews_query.all()
        for review in reviews:
            activities.append({
                'type': 'review',
                'id': f'review_{review.id}',
                'user_id': review.user_id,
                'username': review.user.username,
                'user_avatar': review.user.profile_picture,
                'timestamp': review.created_at.isoformat(),
                'timestamp_raw': review.created_at,
                'media_id': review.media_id,
                'media_type': review.media_type,
                'media_title': review.media_title,
                'rating': review.rating,
                'content': review.content,
                'likes_count': 0  # Can be enhanced later
            })
    
    # Fetch Likes
    if 'likes' in activity_types:
        likes_query = MediaLike.query
        likes_query = user_filter(likes_query, MediaLike.user_id)
        if since_date:
            likes_query = likes_query.filter(MediaLike.created_at >= since_date)
        
        likes = likes_query.all()
        for like in likes:
            activities.append({
                'type': 'like',
                'id': f'like_{like.id}',
                'user_id': like.user_id,
                'username': like.user.username,
                'user_avatar': like.user.profile_picture,
                'timestamp': like.created_at.isoformat(),
                'timestamp_raw': like.created_at,
                'media_id': like.media_id,
                'media_type': like.media_type
            })
    
    # Fetch Comments
    if 'comments' in activity_types:
        comments_query = MediaComment.query.filter_by(is_deleted=False)
        comments_query = user_filter(comments_query, MediaComment.user_id)
        if since_date:
            comments_query = comments_query.filter(MediaComment.created_at >= since_date)
        
        comments = comments_query.all()
        for comment in comments:
            activities.append({
                'type': 'comment',
                'id': f'comment_{comment.id}',
                'user_id': comment.user_id,
                'username': comment.user.username,
                'user_avatar': comment.user.profile_picture,
                'timestamp': comment.created_at.isoformat(),
                'timestamp_raw': comment.created_at,
                'media_id': comment.media_id,
                'media_type': comment.media_type,
                'content': comment.content
            })
    
    # Fetch Tags
    if 'tags' in activity_types:
        tags_query = UserMediaTag.query
        tags_query = user_filter(tags_query, UserMediaTag.user_id)
        if since_date:
            tags_query = tags_query.filter(UserMediaTag.created_at >= since_date)
        
        tags = tags_query.all()
        for tag in tags:
            activities.append({
                'type': 'tag',
                'id': f'tag_{tag.id}',
                'user_id': tag.user_id,
                'username': tag.user.username,
                'user_avatar': tag.user.profile_picture,
                'timestamp': tag.created_at.isoformat(),
                'timestamp_raw': tag.created_at,
                'media_id': tag.media_id,
                'media_type': tag.media_type,
                'tag_name': tag.tag.name
            })
    
    # Sort activities
    if sort_by == 'recent':
        activities.sort(key=lambda x: x['timestamp_raw'], reverse=True)
    elif sort_by == 'popular':
        # For now, just use recent. Can be enhanced with engagement metrics
        activities.sort(key=lambda x: x.get('likes_count', 0), reverse=True)
    
    # Remove timestamp_raw before returning
    for activity in activities:
        activity.pop('timestamp_raw', None)
    
    # Paginate
    total = len(activities)
    start = (page - 1) * per_page
    end = start + per_page
    paginated_activities = activities[start:end]
    
    return jsonify({
        'success': True,
        'activities': paginated_activities,
        'total': total,
        'pages': (total + per_page - 1) // per_page,
        'current_page': page,
        'has_next': end < total,
        'has_prev': page > 1
    })


@activity_feed.route('/api/feed/stats', methods=['GET'])
@login_required
def get_feed_stats():
    """Get statistics about activity feed"""
    feed_type = request.args.get('feed_type', 'following')
    
    # Determine user scope
    if feed_type == 'following':
        following_ids = [f.following_id for f in current_user.following if f.is_active]
        if not following_ids:
            return jsonify({
                'success': True,
                'stats': {
                    'total_reviews': 0,
                    'total_likes': 0,
                    'total_comments': 0,
                    'total_tags': 0,
                    'active_users': 0
                }
            })
        user_filter = lambda query, user_field: query.filter(user_field.in_(following_ids))
    elif feed_type == 'personal':
        user_filter = lambda query, user_field: query.filter(user_field == current_user.id)
    else:  # global
        user_filter = lambda query, user_field: query
    
    # Count activities
    reviews_query = Review.query.filter_by(is_deleted=False)
    reviews_count = user_filter(reviews_query, Review.user_id).count()
    
    likes_query = MediaLike.query
    likes_count = user_filter(likes_query, MediaLike.user_id).count()
    
    comments_query = MediaComment.query.filter_by(is_deleted=False)
    comments_count = user_filter(comments_query, MediaComment.user_id).count()
    
    tags_query = UserMediaTag.query
    tags_count = user_filter(tags_query, UserMediaTag.user_id).count()
    
    # Count active users (those with any activity)
    if feed_type == 'following':
        active_users = len(following_ids)
    elif feed_type == 'personal':
        active_users = 1
    else:
        active_users = User.query.count()
    
    return jsonify({
        'success': True,
        'stats': {
            'total_reviews': reviews_count,
            'total_likes': likes_count,
            'total_comments': comments_count,
            'total_tags': tags_count,
            'active_users': active_users
        }
    })
