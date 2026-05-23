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


def _collect_activities(user_filter, activity_types, since_date):
    """Fetch and serialize all activity items within scope, applying optional time filter."""
    activities = []

    if 'reviews' in activity_types:
        q = Review.query.filter_by(is_deleted=False)
        q = user_filter(q, Review.user_id)
        if since_date:
            q = q.filter(Review.created_at >= since_date)
        for r in q.all():
            activities.append({
                'type': 'review', 'id': f'review_{r.id}',
                'user_id': r.user_id, 'username': r.user.username,
                'user_avatar': r.user.profile_picture,
                'timestamp': r.created_at.isoformat(), 'timestamp_raw': r.created_at,
                'media_id': r.media_id, 'media_type': r.media_type,
                'media_title': r.media_title, 'rating': r.rating,
                'content': r.content, 'likes_count': 0,
            })

    if 'likes' in activity_types:
        q = user_filter(MediaLike.query, MediaLike.user_id)
        if since_date:
            q = q.filter(MediaLike.created_at >= since_date)
        for lk in q.all():
            activities.append({
                'type': 'like', 'id': f'like_{lk.id}',
                'user_id': lk.user_id, 'username': lk.user.username,
                'user_avatar': lk.user.profile_picture,
                'timestamp': lk.created_at.isoformat(), 'timestamp_raw': lk.created_at,
                'media_id': lk.media_id, 'media_type': lk.media_type,
            })

    if 'comments' in activity_types:
        q = MediaComment.query.filter_by(is_deleted=False)
        q = user_filter(q, MediaComment.user_id)
        if since_date:
            q = q.filter(MediaComment.created_at >= since_date)
        for c in q.all():
            activities.append({
                'type': 'comment', 'id': f'comment_{c.id}',
                'user_id': c.user_id, 'username': c.user.username,
                'user_avatar': c.user.profile_picture,
                'timestamp': c.created_at.isoformat(), 'timestamp_raw': c.created_at,
                'media_id': c.media_id, 'media_type': c.media_type,
                'content': c.content,
            })

    if 'tags' in activity_types:
        q = user_filter(UserMediaTag.query, UserMediaTag.user_id)
        if since_date:
            q = q.filter(UserMediaTag.created_at >= since_date)
        for t in q.all():
            activities.append({
                'type': 'tag', 'id': f'tag_{t.id}',
                'user_id': t.user_id, 'username': t.user.username,
                'user_avatar': t.user.profile_picture,
                'timestamp': t.created_at.isoformat(), 'timestamp_raw': t.created_at,
                'media_id': t.media_id, 'media_type': t.media_type,
                'tag_name': t.tag.name,
            })

    return activities


@activity_feed.route('/api/feed/enhanced', methods=['GET'])
@login_required
def get_enhanced_feed():
    feed_type = request.args.get('feed_type', 'following')
    activity_types = request.args.get('activity_types', 'reviews,likes,comments,tags').split(',')
    page = request.args.get('page', 1, type=int)
    per_page = request.args.get('per_page', 20, type=int)
    sort_by = request.args.get('sort', 'recent')
    time_range = request.args.get('time_range', 'all')

    since_date = None
    if time_range == 'day':
        since_date = datetime.utcnow() - timedelta(days=1)
    elif time_range == 'week':
        since_date = datetime.utcnow() - timedelta(days=7)
    elif time_range == 'month':
        since_date = datetime.utcnow() - timedelta(days=30)

    if feed_type == 'following':
        following_ids = [f.following_id for f in current_user.following if f.is_active]
        if not following_ids:
            return jsonify({'success': True, 'activities': [], 'total': 0, 'pages': 0, 'current_page': page})
        user_filter = lambda query, user_field: query.filter(user_field.in_(following_ids))
    elif feed_type == 'personal':
        user_filter = lambda query, user_field: query.filter(user_field == current_user.id)
    else:
        user_filter = lambda query, user_field: query

    activities = _collect_activities(user_filter, activity_types, since_date)

    if sort_by == 'recent':
        activities.sort(key=lambda x: x['timestamp_raw'], reverse=True)
    elif sort_by == 'popular':
        activities.sort(key=lambda x: x.get('likes_count', 0), reverse=True)

    for a in activities:
        a.pop('timestamp_raw', None)

    total = len(activities)
    start = (page - 1) * per_page
    end = start + per_page

    return jsonify({
        'success': True,
        'activities': activities[start:end],
        'total': total,
        'pages': (total + per_page - 1) // per_page,
        'current_page': page,
        'has_next': end < total,
        'has_prev': page > 1,
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
