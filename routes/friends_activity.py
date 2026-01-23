"""
Friends Activity on Media Pages
Shows what friends have done with specific media (reviews, ratings, tags, etc.)
"""
from flask import Blueprint, request, jsonify
from flask_login import login_required, current_user
from models import db, Review, MediaLike, MediaComment, UserMediaTag, UserFollow, user_watchlist, user_viewed
from sqlalchemy import and_

friends_activity = Blueprint('friends_activity', __name__)


@friends_activity.route('/api/media/<int:media_id>/<media_type>/friends-activity', methods=['GET'])
@login_required
def get_friends_activity_for_media(media_id, media_type):
    """
    Get all friend activity for a specific media item
    Returns reviews, likes, comments, tags, and watchlist status from friends
    """
    # Get list of users the current user follows
    following_ids = [f.following_id for f in current_user.following if f.is_active]
    
    if not following_ids:
        return jsonify({
            'success': True,
            'has_friends': False,
            'message': 'You are not following anyone yet',
            'activity': {
                'reviews': [],
                'likes': [],
                'comments': [],
                'tags': [],
                'watchlisted': [],
                'watched': []
            }
        })
    
    activity = {
        'reviews': [],
        'likes': [],
        'comments': [],
        'tags': [],
        'watchlisted': [],
        'watched': []
    }
    
    # Get friend reviews
    reviews = Review.query.filter(
        Review.media_id == media_id,
        Review.media_type == media_type,
        Review.user_id.in_(following_ids),
        Review.is_deleted == False
    ).all()
    
    for review in reviews:
        activity['reviews'].append({
            'user_id': review.user_id,
            'username': review.user.username,
            'user_avatar': review.user.profile_picture,
            'rating': review.rating,
            'content': review.content,
            'created_at': review.created_at.isoformat()
        })
    
    # Get friend likes
    likes = MediaLike.query.filter(
        MediaLike.media_id == media_id,
        MediaLike.media_type == media_type,
        MediaLike.user_id.in_(following_ids)
    ).all()
    
    for like in likes:
        activity['likes'].append({
            'user_id': like.user_id,
            'username': like.user.username,
            'user_avatar': like.user.profile_picture,
            'created_at': like.created_at.isoformat()
        })
    
    # Get friend comments
    comments = MediaComment.query.filter(
        MediaComment.media_id == media_id,
        MediaComment.media_type == media_type,
        MediaComment.user_id.in_(following_ids),
        MediaComment.is_deleted == False
    ).all()
    
    for comment in comments:
        activity['comments'].append({
            'user_id': comment.user_id,
            'username': comment.user.username,
            'user_avatar': comment.user.profile_picture,
            'content': comment.content,
            'created_at': comment.created_at.isoformat()
        })
    
    # Get friend tags
    tags = UserMediaTag.query.filter(
        UserMediaTag.media_id == media_id,
        UserMediaTag.media_type == media_type,
        UserMediaTag.user_id.in_(following_ids)
    ).all()
    
    # Group tags by user
    user_tags = {}
    for tag in tags:
        if tag.user_id not in user_tags:
            user_tags[tag.user_id] = {
                'user_id': tag.user_id,
                'username': tag.user.username,
                'user_avatar': tag.user.profile_picture,
                'tags': []
            }
        user_tags[tag.user_id]['tags'].append(tag.tag.name)
    
    activity['tags'] = list(user_tags.values())
    
    # Get friends who have this in watchlist
    watchlist_query = db.session.query(user_watchlist).filter(
        and_(
            user_watchlist.c.user_id.in_(following_ids),
            user_watchlist.c.media_id == media_id,
            user_watchlist.c.media_type == media_type
        )
    ).all()
    
    from models import User
    watchlisted_user_ids = [row.user_id for row in watchlist_query]
    watchlisted_users = User.query.filter(User.id.in_(watchlisted_user_ids)).all()
    
    for user in watchlisted_users:
        activity['watchlisted'].append({
            'user_id': user.id,
            'username': user.username,
            'user_avatar': user.profile_picture
        })
    
    # Get friends who have watched this
    viewed_query = db.session.query(user_viewed).filter(
        and_(
            user_viewed.c.user_id.in_(following_ids),
            user_viewed.c.media_id == media_id,
            user_viewed.c.media_type == media_type
        )
    ).all()
    
    viewed_user_ids = [row.user_id for row in viewed_query]
    viewed_users = User.query.filter(User.id.in_(viewed_user_ids)).all()
    
    for user in viewed_users:
        # Get the rating if available
        rating_row = next((r for r in viewed_query if r.user_id == user.id), None)
        activity['watched'].append({
            'user_id': user.id,
            'username': user.username,
            'user_avatar': user.profile_picture,
            'rating': rating_row.rating if rating_row and rating_row.rating else None
        })
    
    # Count total friends engaging
    unique_friends = set()
    for category in activity.values():
        for item in category:
            unique_friends.add(item['user_id'])
    
    return jsonify({
        'success': True,
        'has_friends': True,
        'friends_count': len(following_ids),
        'engaging_friends_count': len(unique_friends),
        'activity': activity
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
