"""
Enhanced Activity Feed Routes
"""
from flask import Blueprint, request, jsonify, render_template
from flask_login import login_required, current_user
from models import User, Review, MediaLike, MediaComment, UserMediaTag
from sqlalchemy.orm import joinedload
from datetime import datetime, timedelta

activity_feed = Blueprint('activity_feed', __name__)


@activity_feed.route('/feed/enhanced')
@login_required
def enhanced_feed_page():
    return render_template('feed_enhanced.html')


def _collect_activities(user_filter, activity_types, since_date):
    """Fetch and serialize activity items. Eager-loads user + media to avoid N+1."""
    activities = []

    if 'reviews' in activity_types:
        q = (Review.query
             .filter(Review.is_deleted == False)
             .options(joinedload(Review.user), joinedload(Review.media)))
        q = user_filter(q, Review.user_id)
        if since_date:
            q = q.filter(Review.created_at >= since_date)
        for r in q.all():
            activities.append({
                'type': 'review', 'id': f'review_{r.id}',
                'user_id': r.user_id,
                'username': r.user.username if r.user else 'Unknown',
                'user_avatar': r.user.profile_picture if r.user else None,
                'timestamp': r.created_at.isoformat(), 'timestamp_raw': r.created_at,
                'media_id': r.media_id, 'media_type': r.media_type,
                'media_title': r.media.title if r.media else 'Unknown',
                'rating': r.rating,
                'content': r.content, 'likes_count': r.likes_count or 0,
            })

    if 'likes' in activity_types:
        q = MediaLike.query.options(joinedload(MediaLike.user))
        q = user_filter(q, MediaLike.user_id)
        if since_date:
            q = q.filter(MediaLike.created_at >= since_date)
        for lk in q.all():
            activities.append({
                'type': 'like', 'id': f'like_{lk.id}',
                'user_id': lk.user_id,
                'username': lk.user.username if lk.user else 'Unknown',
                'user_avatar': lk.user.profile_picture if lk.user else None,
                'timestamp': lk.created_at.isoformat(), 'timestamp_raw': lk.created_at,
                'media_id': lk.media_id, 'media_type': lk.media_type,
            })

    if 'comments' in activity_types:
        q = (MediaComment.query
             .filter(MediaComment.is_deleted == False)
             .options(joinedload(MediaComment.user)))
        q = user_filter(q, MediaComment.user_id)
        if since_date:
            q = q.filter(MediaComment.created_at >= since_date)
        for c in q.all():
            activities.append({
                'type': 'comment', 'id': f'comment_{c.id}',
                'user_id': c.user_id,
                'username': c.user.username if c.user else 'Unknown',
                'user_avatar': c.user.profile_picture if c.user else None,
                'timestamp': c.created_at.isoformat(), 'timestamp_raw': c.created_at,
                'media_id': c.media_id, 'media_type': c.media_type,
                'content': c.content,
            })

    if 'tags' in activity_types:
        q = UserMediaTag.query.options(joinedload(UserMediaTag.user), joinedload(UserMediaTag.tag))
        q = user_filter(q, UserMediaTag.user_id)
        if since_date:
            q = q.filter(UserMediaTag.created_at >= since_date)
        for t in q.all():
            activities.append({
                'type': 'tag', 'id': f'tag_{t.id}',
                'user_id': t.user_id,
                'username': t.user.username if t.user else 'Unknown',
                'user_avatar': t.user.profile_picture if t.user else None,
                'timestamp': t.created_at.isoformat(), 'timestamp_raw': t.created_at,
                'media_id': t.media_id, 'media_type': t.media_type,
                'tag_name': t.tag.name if t.tag else '',
            })

    return activities


@activity_feed.route('/api/feed/enhanced', methods=['GET'])
@login_required
def get_enhanced_feed():
    feed_type = request.args.get('feed_type', 'following')
    activity_types = request.args.get('activity_types', 'reviews,likes,comments,tags').split(',')
    page = request.args.get('page', 1, type=int)
    per_page = min(request.args.get('per_page', 20, type=int), 100)
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
    feed_type = request.args.get('feed_type', 'following')

    if feed_type == 'following':
        following_ids = [f.following_id for f in current_user.following if f.is_active]
        if not following_ids:
            return jsonify({'success': True, 'stats': {
                'total_reviews': 0, 'total_likes': 0,
                'total_comments': 0, 'total_tags': 0, 'active_users': 0
            }})
        user_filter = lambda query, user_field: query.filter(user_field.in_(following_ids))
        active_users = len(following_ids)
    elif feed_type == 'personal':
        user_filter = lambda query, user_field: query.filter(user_field == current_user.id)
        active_users = 1
    else:
        user_filter = lambda query, user_field: query
        active_users = User.query.count()

    reviews_count = user_filter(
        Review.query.filter(Review.is_deleted == False), Review.user_id
    ).count()
    likes_count = user_filter(MediaLike.query, MediaLike.user_id).count()
    comments_count = user_filter(
        MediaComment.query.filter(MediaComment.is_deleted == False), MediaComment.user_id
    ).count()
    tags_count = user_filter(UserMediaTag.query, UserMediaTag.user_id).count()

    return jsonify({'success': True, 'stats': {
        'total_reviews': reviews_count,
        'total_likes': likes_count,
        'total_comments': comments_count,
        'total_tags': tags_count,
        'active_users': active_users,
    }})
