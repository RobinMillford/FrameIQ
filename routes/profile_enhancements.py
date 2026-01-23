"""
Profile Enhancements
Adds badges, achievements, and enhanced statistics to user profiles
"""
from flask import Blueprint, request, jsonify
from flask_login import login_required, current_user
from models import db, User, Review, MediaLike, MediaComment, UserMediaTag, user_watchlist, user_viewed
from sqlalchemy import func, desc
from datetime import datetime, timedelta

profile_enhancements = Blueprint('profile_enhancements', __name__)


def calculate_user_badges(user_id):
    """Calculate all badges earned by a user"""
    badges = []
    
    # Review badges
    review_count = Review.query.filter_by(user_id=user_id, is_deleted=False).count()
    if review_count >= 1:
        badges.append({'id': 'first_review', 'name': 'First Steps', 'icon': '📝', 'description': 'Posted your first review'})
    if review_count >= 10:
        badges.append({'id': 'reviewer_10', 'name': 'Critic', 'icon': '🎬', 'description': '10+ reviews written'})
    if review_count >= 50:
        badges.append({'id': 'reviewer_50', 'name': 'Super Critic', 'icon': '⭐', 'description': '50+ reviews written'})
    if review_count >= 100:
        badges.append({'id': 'reviewer_100', 'name': 'Master Critic', 'icon': '🏆', 'description': '100+ reviews written'})
    
    # Social badges
    user = User.query.get(user_id)
    if user:
        followers_count = user.followers_count or 0
        if followers_count >= 10:
            badges.append({'id': 'social_10', 'name': 'Popular', 'icon': '👥', 'description': '10+ followers'})
        if followers_count >= 50:
            badges.append({'id': 'social_50', 'name': 'Influencer', 'icon': '🌟', 'description': '50+ followers'})
        if followers_count >= 100:
            badges.append({'id': 'social_100', 'name': 'Celebrity', 'icon': '💫', 'description': '100+ followers'})
    
    # Activity badges
    like_count = MediaLike.query.filter_by(user_id=user_id).count()
    if like_count >= 50:
        badges.append({'id': 'likes_50', 'name': 'Heart Giver', 'icon': '❤️', 'description': '50+ likes given'})
    
    comment_count = MediaComment.query.filter_by(user_id=user_id, is_deleted=False).count()
    if comment_count >= 50:
        badges.append({'id': 'comments_50', 'name': 'Conversationalist', 'icon': '💬', 'description': '50+ comments posted'})
    
    # Tag badges
    tag_count = UserMediaTag.query.filter_by(user_id=user_id).count()
    if tag_count >= 25:
        badges.append({'id': 'tags_25', 'name': 'Organizer', 'icon': '🏷️', 'description': '25+ tags added'})
    if tag_count >= 100:
        badges.append({'id': 'tags_100', 'name': 'Master Organizer', 'icon': '📋', 'description': '100+ tags added'})
    
    # Watchlist badges
    watchlist_count = db.session.query(user_watchlist).filter(user_watchlist.c.user_id == user_id).count()
    if watchlist_count >= 25:
        badges.append({'id': 'watchlist_25', 'name': 'Collector', 'icon': '📺', 'description': '25+ items in watchlist'})
    if watchlist_count >= 100:
        badges.append({'id': 'watchlist_100', 'name': 'Mega Collector', 'icon': '🎞️', 'description': '100+ items in watchlist'})
    
    # Watched badges
    viewed_count = db.session.query(user_viewed).filter(user_viewed.c.user_id == user_id).count()
    if viewed_count >= 50:
        badges.append({'id': 'viewed_50', 'name': 'Binge Watcher', 'icon': '📹', 'description': '50+ items watched'})
    if viewed_count >= 100:
        badges.append({'id': 'viewed_100', 'name': 'Cinephile', 'icon': '🎥', 'description': '100+ items watched'})
    if viewed_count >= 500:
        badges.append({'id': 'viewed_500', 'name': 'Ultimate Cinephile', 'icon': '🎭', 'description': '500+ items watched'})
    
    # Time-based badges
    if user:
        account_age_days = (datetime.utcnow() - user.date_joined).days
        if account_age_days >= 30:
            badges.append({'id': 'member_30', 'name': 'Member', 'icon': '🎫', 'description': '30+ days on FrameIQ'})
        if account_age_days >= 365:
            badges.append({'id': 'member_365', 'name': 'Veteran', 'icon': '🎖️', 'description': '1+ year on FrameIQ'})
    
    # Activity streak badges (simplified - checks if active in last 7 days)
    recent_activity = Review.query.filter(
        Review.user_id == user_id,
        Review.created_at >= datetime.utcnow() - timedelta(days=7)
    ).count()
    if recent_activity >= 3:
        badges.append({'id': 'active_streak', 'name': 'Active This Week', 'icon': '🔥', 'description': '3+ activities this week'})
    
    return badges


@profile_enhancements.route('/api/users/<int:user_id>/badges', methods=['GET'])
def get_user_badges(user_id):
    """Get all badges earned by a user with progress tracking"""
    user = User.query.get(user_id)
    if not user:
        return jsonify({'error': 'User not found'}), 404
    
    # Get current counts for progress tracking
    review_count = Review.query.filter_by(user_id=user_id, is_deleted=False).count()
    like_count = MediaLike.query.filter_by(user_id=user_id).count()
    comment_count = MediaComment.query.filter_by(user_id=user_id, is_deleted=False).count()
    tag_count = UserMediaTag.query.filter_by(user_id=user_id).count()
    watchlist_count = db.session.query(user_watchlist).filter(user_watchlist.c.user_id == user_id).count()
    viewed_count = db.session.query(user_viewed).filter(user_viewed.c.user_id == user_id).count()
    followers_count = user.followers_count or 0
    
    # Define all possible badges with progress
    all_badges = []
    
    # Review badges with progress
    review_milestones = [
        (1, 'first_review', 'First Steps', '📝', 'Posted your first review'),
        (10, 'reviewer_10', 'Critic', '🎬', '10+ reviews written'),
        (50, 'reviewer_50', 'Super Critic', '⭐', '50+ reviews written'),
        (100, 'reviewer_100', 'Master Critic', '🏆', '100+ reviews written'),
    ]
    
    for target, badge_id, name, icon, desc in review_milestones:
        all_badges.append({
            'id': badge_id,
            'name': name,
            'icon': icon,
            'description': desc,
            'earned': review_count >= target,
            'progress': min(100, (review_count / target) * 100),
            'current': review_count,
            'target': target,
            'category': 'reviews'
        })
    
    # Social badges with progress
    social_milestones = [
        (10, 'social_10', 'Popular', '👥', '10+ followers'),
        (50, 'social_50', 'Influencer', '🌟', '50+ followers'),
        (100, 'social_100', 'Celebrity', '💫', '100+ followers'),
    ]
    
    for target, badge_id, name, icon, desc in social_milestones:
        all_badges.append({
            'id': badge_id,
            'name': name,
            'icon': icon,
            'description': desc,
            'earned': followers_count >= target,
            'progress': min(100, (followers_count / target) * 100),
            'current': followers_count,
            'target': target,
            'category': 'social'
        })
    
    # Activity badges
    if like_count >= 50:
        all_badges.append({
            'id': 'likes_50',
            'name': 'Heart Giver',
            'icon': '❤️',
            'description': '50+ likes given',
            'earned': True,
            'category': 'activity'
        })
    else:
        all_badges.append({
            'id': 'likes_50',
            'name': 'Heart Giver',
            'icon': '❤️',
            'description': '50+ likes given',
            'earned': False,
            'progress': min(100, (like_count / 50) * 100),
            'current': like_count,
            'target': 50,
            'category': 'activity'
        })
    
    if comment_count >= 50:
        all_badges.append({
            'id': 'comments_50',
            'name': 'Conversationalist',
            'icon': '💬',
            'description': '50+ comments posted',
            'earned': True,
            'category': 'activity'
        })
    else:
        all_badges.append({
            'id': 'comments_50',
            'name': 'Conversationalist',
            'icon': '💬',
            'description': '50+ comments posted',
            'earned': False,
            'progress': min(100, (comment_count / 50) * 100),
            'current': comment_count,
            'target': 50,
            'category': 'activity'
        })
    
    # Tag badges
    if tag_count >= 25:
        all_badges.append({
            'id': 'tags_25',
            'name': 'Organizer',
            'icon': '🏷️',
            'description': '25+ tags added',
            'earned': True,
            'category': 'tags'
        })
    else:
        all_badges.append({
            'id': 'tags_25',
            'name': 'Organizer',
            'icon': '🏷️',
            'description': '25+ tags added',
            'earned': False,
            'progress': min(100, (tag_count / 25) * 100),
            'current': tag_count,
            'target': 25,
            'category': 'tags'
        })
    
    # Watched badges
    watched_milestones = [
        (50, 'viewed_50', 'Binge Watcher', '📹', '50+ items watched'),
        (100, 'viewed_100', 'Cinephile', '🎥', '100+ items watched'),
        (500, 'viewed_500', 'Ultimate Cinephile', '🎭', '500+ items watched'),
    ]
    
    for target, badge_id, name, icon, desc in watched_milestones:
        all_badges.append({
            'id': badge_id,
            'name': name,
            'icon': icon,
            'description': desc,
            'earned': viewed_count >= target,
            'progress': min(100, (viewed_count / target) * 100),
            'current': viewed_count,
            'target': target,
            'category': 'watched'
        })
    
    # Sort: earned badges first, then by progress
    all_badges.sort(key=lambda x: (not x['earned'], -x.get('progress', 100)))
    
    earned_count = sum(1 for b in all_badges if b['earned'])
    
    return jsonify({
        'success': True,
        'user_id': user_id,
        'username': user.username,
        'total_badges': len(all_badges),
        'total_earned': earned_count,
        'badges': all_badges
    })


@profile_enhancements.route('/api/users/<int:user_id>/stats/enhanced', methods=['GET'])
def get_enhanced_stats(user_id):
    """Get enhanced statistics for a user"""
    user = User.query.get(user_id)
    if not user:
        return jsonify({'error': 'User not found'}), 404
    
    # Basic counts
    review_count = Review.query.filter_by(user_id=user_id, is_deleted=False).count()
    like_count = MediaLike.query.filter_by(user_id=user_id).count()
    comment_count = MediaComment.query.filter_by(user_id=user_id, is_deleted=False).count()
    tag_count = UserMediaTag.query.filter_by(user_id=user_id).count()
    watchlist_count = db.session.query(user_watchlist).filter(user_watchlist.c.user_id == user_id).count()
    viewed_count = db.session.query(user_viewed).filter(user_viewed.c.user_id == user_id).count()
    
    # Rating analysis
    reviews_with_ratings = Review.query.filter(
        Review.user_id == user_id,
        Review.is_deleted == False,
        Review.rating.isnot(None)
    ).all()
    
    avg_rating = None
    rating_distribution = {i: 0 for i in range(1, 11)}
    if reviews_with_ratings:
        avg_rating = sum(r.rating for r in reviews_with_ratings) / len(reviews_with_ratings)
        for review in reviews_with_ratings:
            rating_distribution[review.rating] += 1
    
    # Activity timeline (last 30 days)
    thirty_days_ago = datetime.utcnow() - timedelta(days=30)
    recent_reviews = Review.query.filter(
        Review.user_id == user_id,
        Review.created_at >= thirty_days_ago
    ).count()
    recent_likes = MediaLike.query.filter(
        MediaLike.user_id == user_id,
        MediaLike.created_at >= thirty_days_ago
    ).count()
    recent_comments = MediaComment.query.filter(
        MediaComment.user_id == user_id,
        MediaComment.created_at >= thirty_days_ago
    ).count()
    
    # Most used tags
    most_used_tags = db.session.query(
        UserMediaTag.tag_id,
        func.count(UserMediaTag.id).label('count')
    ).filter(
        UserMediaTag.user_id == user_id
    ).group_by(UserMediaTag.tag_id).order_by(desc('count')).limit(5).all()
    
    from models import Tag
    top_tags = []
    for tag_id, count in most_used_tags:
        tag = Tag.query.get(tag_id)
        if tag:
            top_tags.append({'name': tag.name, 'count': count})
    
    # Account age
    account_age_days = (datetime.utcnow() - user.date_joined).days
    
    return jsonify({
        'success': True,
        'user_id': user_id,
        'username': user.username,
        'stats': {
            'total_reviews': review_count,
            'total_likes': like_count,
            'total_comments': comment_count,
            'total_tags': tag_count,
            'watchlist_count': watchlist_count,
            'viewed_count': viewed_count,
            'followers_count': user.followers_count or 0,
            'following_count': user.following_count or 0,
            'avg_rating': round(avg_rating, 1) if avg_rating else None,
            'rating_distribution': rating_distribution,
            'recent_activity_30d': {
                'reviews': recent_reviews,
                'likes': recent_likes,
                'comments': recent_comments
            },
            'top_tags': top_tags,
            'account_age_days': account_age_days,
            'member_since': user.date_joined.isoformat()
        }
    })


@profile_enhancements.route('/api/users/<int:user_id>/achievements', methods=['GET'])
def get_user_achievements(user_id):
    """Get a summary of user achievements and progress"""
    user = User.query.get(user_id)
    if not user:
        return jsonify({'error': 'User not found'}), 404
    
    badges = calculate_user_badges(user_id)
    
    # Calculate progress towards next badges
    review_count = Review.query.filter_by(user_id=user_id, is_deleted=False).count()
    viewed_count = db.session.query(user_viewed).filter(user_viewed.c.user_id == user_id).count()
    followers_count = user.followers_count or 0
    
    progress = []
    
    # Next review milestone
    if review_count < 10:
        progress.append({
            'goal': 'Write 10 reviews',
            'current': review_count,
            'target': 10,
            'percentage': int((review_count / 10) * 100)
        })
    elif review_count < 50:
        progress.append({
            'goal': 'Write 50 reviews',
            'current': review_count,
            'target': 50,
            'percentage': int((review_count / 50) * 100)
        })
    elif review_count < 100:
        progress.append({
            'goal': 'Write 100 reviews',
            'current': review_count,
            'target': 100,
            'percentage': int((review_count / 100) * 100)
        })
    
    # Next viewing milestone
    if viewed_count < 50:
        progress.append({
            'goal': 'Watch 50 items',
            'current': viewed_count,
            'target': 50,
            'percentage': int((viewed_count / 50) * 100)
        })
    elif viewed_count < 100:
        progress.append({
            'goal': 'Watch 100 items',
            'current': viewed_count,
            'target': 100,
            'percentage': int((viewed_count / 100) * 100)
        })
    elif viewed_count < 500:
        progress.append({
            'goal': 'Watch 500 items',
            'current': viewed_count,
            'target': 500,
            'percentage': int((viewed_count / 500) * 100)
        })
    
    # Next social milestone
    if followers_count < 10:
        progress.append({
            'goal': 'Get 10 followers',
            'current': followers_count,
            'target': 10,
            'percentage': int((followers_count / 10) * 100)
        })
    elif followers_count < 50:
        progress.append({
            'goal': 'Get 50 followers',
            'current': followers_count,
            'target': 50,
            'percentage': int((followers_count / 50) * 100)
        })
    
    return jsonify({
        'success': True,
        'user_id': user_id,
        'username': user.username,
        'total_badges': len(badges),
        'badges': badges,
        'progress': progress
    })
