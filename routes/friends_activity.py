"""
Friends Activity on Media Pages
Shows what friends have done with specific media (reviews, ratings, tags, etc.)
"""
from flask import Blueprint, jsonify
from flask_login import login_required, current_user
from models import db, User, Review, MediaLike, MediaComment, UserMediaTag, user_watchlist, user_viewed
from sqlalchemy import and_

friends_activity = Blueprint('friends_activity', __name__)

_EMPTY_ACTIVITY = {'reviews': [], 'likes': [], 'comments': [], 'tags': [], 'watchlisted': [], 'watched': []}


def _friend_reviews(media_id, media_type, following_ids):
    return [
        {'user_id': r.user_id, 'username': r.user.username, 'user_avatar': r.user.profile_picture,
         'rating': r.rating, 'content': r.content, 'created_at': r.created_at.isoformat()}
        for r in Review.query.filter(
            Review.media_id == media_id, Review.media_type == media_type,
            Review.user_id.in_(following_ids), Review.is_deleted == False,
        ).all()
    ]


def _friend_likes(media_id, media_type, following_ids):
    return [
        {'user_id': lk.user_id, 'username': lk.user.username, 'user_avatar': lk.user.profile_picture,
         'created_at': lk.created_at.isoformat()}
        for lk in MediaLike.query.filter(
            MediaLike.media_id == media_id, MediaLike.media_type == media_type,
            MediaLike.user_id.in_(following_ids),
        ).all()
    ]


def _friend_comments(media_id, media_type, following_ids):
    return [
        {'user_id': c.user_id, 'username': c.user.username, 'user_avatar': c.user.profile_picture,
         'content': c.content, 'created_at': c.created_at.isoformat()}
        for c in MediaComment.query.filter(
            MediaComment.media_id == media_id, MediaComment.media_type == media_type,
            MediaComment.user_id.in_(following_ids), MediaComment.is_deleted == False,
        ).all()
    ]


def _friend_tags(media_id, media_type, following_ids):
    user_tags = {}
    for tag in UserMediaTag.query.filter(
        UserMediaTag.media_id == media_id, UserMediaTag.media_type == media_type,
        UserMediaTag.user_id.in_(following_ids),
    ).all():
        entry = user_tags.setdefault(tag.user_id, {
            'user_id': tag.user_id, 'username': tag.user.username,
            'user_avatar': tag.user.profile_picture, 'tags': [],
        })
        entry['tags'].append(tag.tag.name)
    return list(user_tags.values())


def _friend_list_users(table, media_id, media_type, following_ids):
    """Query an association table and return (User list, {user_id: row} map)."""
    rows = db.session.query(table).filter(
        and_(table.c.user_id.in_(following_ids),
             table.c.media_id == media_id,
             table.c.media_type == media_type)
    ).all()
    row_by_uid = {r.user_id: r for r in rows}
    users = User.query.filter(User.id.in_(row_by_uid.keys())).all()
    return users, row_by_uid


@friends_activity.route('/api/media/<int:media_id>/<media_type>/friends-activity', methods=['GET'])
@login_required
def get_friends_activity_for_media(media_id, media_type):
    following_ids = [f.following_id for f in current_user.following if f.is_active]
    if not following_ids:
        return jsonify({'success': True, 'has_friends': False,
                        'message': 'You are not following anyone yet', 'activity': _EMPTY_ACTIVITY})

    watchlist_users, _ = _friend_list_users(user_watchlist, media_id, media_type, following_ids)
    viewed_users, viewed_map = _friend_list_users(user_viewed, media_id, media_type, following_ids)

    activity = {
        'reviews':    _friend_reviews(media_id, media_type, following_ids),
        'likes':      _friend_likes(media_id, media_type, following_ids),
        'comments':   _friend_comments(media_id, media_type, following_ids),
        'tags':       _friend_tags(media_id, media_type, following_ids),
        'watchlisted': [{'user_id': u.id, 'username': u.username, 'user_avatar': u.profile_picture}
                        for u in watchlist_users],
        'watched': [
            {'user_id': u.id, 'username': u.username, 'user_avatar': u.profile_picture,
             'rating': getattr(viewed_map.get(u.id), 'rating', None) or None}
            for u in viewed_users
        ],
    }

    unique_friends = {item['user_id'] for cat in activity.values() for item in cat}

    return jsonify({
        'success': True, 'has_friends': True,
        'friends_count': len(following_ids),
        'engaging_friends_count': len(unique_friends),
        'activity': activity,
    })


@friends_activity.route('/api/media/<int:media_id>/<media_type>/friends-summary', methods=['GET'])
@login_required
def get_friends_summary(media_id, media_type):
    """
    Get a quick summary of friend activity for a media item
    Useful for displaying compact info on media cards
    """
    following_ids = [f.following_id for f in current_user.following if f.is_active]
    
    if not following_ids:
        return jsonify({
            'success': True,
            'summary': {
                'has_activity': False,
                'review_count': 0,
                'like_count': 0,
                'avg_rating': None,
                'top_friends': []
            }
        })
    
    # Count reviews
    review_count = Review.query.filter(
        Review.media_id == media_id,
        Review.media_type == media_type,
        Review.user_id.in_(following_ids),
        Review.is_deleted == False
    ).count()
    
    # Count likes
    like_count = MediaLike.query.filter(
        MediaLike.media_id == media_id,
        MediaLike.media_type == media_type,
        MediaLike.user_id.in_(following_ids)
    ).count()
    
    # Get average rating from friends' reviews
    reviews_with_ratings = Review.query.filter(
        Review.media_id == media_id,
        Review.media_type == media_type,
        Review.user_id.in_(following_ids),
        Review.is_deleted == False,
        Review.rating.isnot(None)
    ).all()
    
    avg_rating = None
    if reviews_with_ratings:
        avg_rating = sum(r.rating for r in reviews_with_ratings) / len(reviews_with_ratings)
    
    # Get top 3 friends who interacted
    top_friends = []
    from models import User
    
    # Get unique users from reviews and likes
    user_ids = set()
    for review in Review.query.filter(
        Review.media_id == media_id,
        Review.media_type == media_type,
        Review.user_id.in_(following_ids),
        Review.is_deleted == False
    ).limit(3).all():
        user_ids.add(review.user_id)
    
    if len(user_ids) < 3:
        for like in MediaLike.query.filter(
            MediaLike.media_id == media_id,
            MediaLike.media_type == media_type,
            MediaLike.user_id.in_(following_ids)
        ).limit(3).all():
            if len(user_ids) >= 3:
                break
            user_ids.add(like.user_id)
    
    users = User.query.filter(User.id.in_(user_ids)).all()
    top_friends = [{
        'user_id': u.id,
        'username': u.username,
        'user_avatar': u.profile_picture
    } for u in users]
    
    return jsonify({
        'success': True,
        'summary': {
            'has_activity': review_count > 0 or like_count > 0,
            'review_count': review_count,
            'like_count': like_count,
            'avg_rating': round(avg_rating, 1) if avg_rating else None,
            'top_friends': top_friends
        }
    })
