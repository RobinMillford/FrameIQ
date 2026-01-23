"""
Popular with Friends API
Shows which friends have watched/reviewed specific media items
"""
from flask import Blueprint, jsonify
from flask_login import login_required, current_user
from models import db, User, UserFollow, Review, DiaryEntry
from sqlalchemy import and_, func

popular_bp = Blueprint('popular', __name__)


@popular_bp.route('/api/media/<int:media_id>/popular-with-friends', methods=['GET'])
@login_required
def get_popular_with_friends(media_id):
    """Get list of friends who have watched/reviewed this media"""
    try:
        # Get IDs of users current user follows
        following_ids = [f.following_id for f in current_user.following if f.is_active]
        
        if not following_ids:
            return jsonify({
                'success': True,
                'friends': [],
                'total_count': 0,
                'watched_count': 0,
                'reviewed_count': 0,
                'average_rating': None
            })
        
        # Find friends who have diary entries for this media
        friends_watched = db.session.query(
            User,
            DiaryEntry.watched_date,
            DiaryEntry.rating
        ).join(
            DiaryEntry, User.id == DiaryEntry.user_id
        ).filter(
            and_(
                User.id.in_(following_ids),
                DiaryEntry.media_id == media_id
            )
        ).order_by(DiaryEntry.watched_date.desc()).all()
        
        # Find friends who have reviewed this media
        friends_reviewed = db.session.query(
            User,
            Review.rating,
            Review.content,
            Review.created_at
        ).join(
            Review, User.id == Review.user_id
        ).filter(
            and_(
                User.id.in_(following_ids),
                Review.media_id == media_id,
                Review.is_deleted == False
            )
        ).all()
        
        # Combine data - create dict keyed by user_id
        friends_data = {}
        
        for user, watched_date, rating in friends_watched:
            if user.id not in friends_data:
                friends_data[user.id] = {
                    'id': user.id,
                    'username': user.username,
                    'profile_picture': user.profile_picture,
                    'watched_date': watched_date.isoformat() if watched_date else None,
                    'diary_rating': rating,
                    'has_review': False,
                    'review_rating': None,
                    'review_excerpt': None
                }
        
        for user, rating, content, created_at in friends_reviewed:
            if user.id in friends_data:
                friends_data[user.id]['has_review'] = True
                friends_data[user.id]['review_rating'] = rating
                friends_data[user.id]['review_excerpt'] = content[:150] + '...' if content and len(content) > 150 else content
            else:
                friends_data[user.id] = {
                    'id': user.id,
                    'username': user.username,
                    'profile_picture': user.profile_picture,
                    'watched_date': None,
                    'diary_rating': None,
                    'has_review': True,
                    'review_rating': rating,
                    'review_excerpt': content[:150] + '...' if content and len(content) > 150 else content
                }
        
        # Calculate statistics
        all_ratings = []
        for friend in friends_data.values():
            if friend['review_rating']:
                all_ratings.append(friend['review_rating'])
            elif friend['diary_rating']:
                all_ratings.append(friend['diary_rating'])
        
        avg_rating = sum(all_ratings) / len(all_ratings) if all_ratings else None
        
        return jsonify({
            'success': True,
            'friends': list(friends_data.values()),
            'total_count': len(friends_data),
            'watched_count': len(friends_watched),
            'reviewed_count': len(friends_reviewed),
            'average_rating': round(avg_rating, 1) if avg_rating else None
        })
        
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500
