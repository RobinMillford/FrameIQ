"""
Week 4: User Discovery & Search API
Handles user search, suggested follows, and similarity matching
"""
from flask import Blueprint, request, jsonify, render_template
from flask_login import login_required, current_user
from models import db, User, UserFollow, Review, DiaryEntry
from sqlalchemy import func, or_, and_, desc
from datetime import datetime, timedelta
import json

user_discovery = Blueprint('user_discovery', __name__)


# ============================================================================
# PAGE ROUTES
# ============================================================================

@user_discovery.route('/discover')
def discover_page():
    """Render the user discovery page"""
    return render_template('discover_users.html')


# ============================================================================
# USER SEARCH
# ============================================================================

@user_discovery.route('/api/users/search', methods=['GET'])
def search_users():
    """Search for users by username or name"""
    query_str = request.args.get('q', '').strip()
    page = request.args.get('page', 1, type=int)
    per_page = request.args.get('per_page', 20, type=int)
    
    if not query_str:
        return jsonify({'users': [], 'total': 0}), 200
    
    # Search in username, first_name, last_name
    search_filter = or_(
        User.username.ilike(f'%{query_str}%'),
        User.first_name.ilike(f'%{query_str}%'),
        User.last_name.ilike(f'%{query_str}%')
    )
    
    query = User.query.filter(search_filter).filter(User.is_active == True)  # type: ignore
    
    # Exclude current user if authenticated
    if current_user.is_authenticated:
        query = query.filter(User.id != current_user.id)
    
    # Order by relevance (exact matches first, then by followers)
    query = query.order_by(
        User.username.ilike(f'{query_str}%').desc(),  # Starts with query
        User.followers_count.desc(),
        User.username.asc()
    )
    
    # Paginate
    pagination = query.paginate(page=page, per_page=per_page, error_out=False)  # type: ignore
    
    # Build response with follow status
    users_data = []
    for user in pagination.items:
        user_dict = {
            'id': user.id,
            'username': user.username,
            'first_name': user.first_name,
            'last_name': user.last_name,
            'profile_picture': user.profile_picture,
            'bio': user.bio,
            'followers_count': user.followers_count or 0,
            'following_count': user.following_count or 0,
            'total_reviews': user.total_reviews or 0,
            'is_following': False
        }
        
        # Check if current user follows this user
        if current_user.is_authenticated:
            is_following = UserFollow.query.filter_by(
                follower_id=current_user.id,
                following_id=user.id,
                is_active=True
            ).first() is not None
            user_dict['is_following'] = is_following
        
        users_data.append(user_dict)
    
    return jsonify({
        'users': users_data,
        'total': pagination.total,
        'pages': pagination.pages,
        'current_page': page,
        'has_next': pagination.has_next,
        'has_prev': pagination.has_prev
    }), 200


# ============================================================================
# SUGGESTED FOLLOWS
# ============================================================================

@user_discovery.route('/api/users/suggested', methods=['GET'])
@login_required
def suggested_follows():
    """Get suggested users to follow based on various factors"""
    limit = request.args.get('limit', 10, type=int)
    
    # Get users the current user already follows
    following_ids = db.session.query(UserFollow.following_id).filter_by(
        follower_id=current_user.id,
        is_active=True
    ).all()
    following_ids = [fid[0] for fid in following_ids]
    
    # Strategy 1: Users followed by people you follow (friends of friends)
    friends_of_friends = db.session.query(
        UserFollow.following_id,
        func.count(UserFollow.follower_id).label('mutual_count')
    ).filter(
        UserFollow.follower_id.in_(following_ids),
        UserFollow.is_active == True,  # type: ignore
        UserFollow.following_id != current_user.id,
        ~UserFollow.following_id.in_(following_ids)  # Not already following
    ).group_by(UserFollow.following_id).order_by(desc('mutual_count')).limit(5).all()
    
    suggested_ids = [user_id for user_id, _ in friends_of_friends]
    
    # Strategy 2: Popular users (if we need more suggestions)
    if len(suggested_ids) < limit:
        popular_users = User.query.filter(
            User.id != current_user.id,
            ~User.id.in_(following_ids),
            ~User.id.in_(suggested_ids),
            User.is_active == True,  # type: ignore
            User.total_reviews > 5  # Active users only
        ).order_by(
            desc(User.followers_count)
        ).limit(limit - len(suggested_ids)).all()
        
        suggested_ids.extend([u.id for u in popular_users])
    
    # Strategy 3: Users with similar taste (from user_similarity table)
    # This will be populated by a background job, for now skip if empty
    
    # Fetch user details
    if not suggested_ids:
        return jsonify({'users': []}), 200
    
    users = User.query.filter(User.id.in_(suggested_ids)).all()
    
    users_data = []
    for user in users:
        # Calculate reason for suggestion
        mutual_count = next((count for uid, count in friends_of_friends if uid == user.id), 0)
        
        reason = "Popular user"
        if mutual_count > 0:
            reason = f"Followed by {mutual_count} user{'s' if mutual_count > 1 else ''} you follow"
        
        users_data.append({
            'id': user.id,
            'username': user.username,
            'first_name': user.first_name,
            'last_name': user.last_name,
            'profile_picture': user.profile_picture,
            'bio': user.bio,
            'followers_count': user.followers_count or 0,
            'total_reviews': user.total_reviews or 0,
            'reason': reason
        })
    
    return jsonify({'users': users_data}), 200


# ============================================================================
# POPULAR USERS
# ============================================================================

@user_discovery.route('/api/users/popular', methods=['GET'])
def popular_users():
    """Get most popular users by follower count"""
    page = request.args.get('page', 1, type=int)
    per_page = request.args.get('per_page', 20, type=int)
    timeframe = request.args.get('timeframe', 'all')  # all, week, month
    
    query = User.query.filter(User.is_active == True)  # type: ignore
    
    # Exclude current user if authenticated
    if current_user.is_authenticated:
        query = query.filter(User.id != current_user.id)
    
    # Filter by activity timeframe
    if timeframe == 'week':
        week_ago = datetime.utcnow() - timedelta(days=7)
        # Users active in last week
        active_user_ids = db.session.query(Review.user_id).filter(
            Review.created_at >= week_ago
        ).union(
            db.session.query(DiaryEntry.user_id).filter(
                DiaryEntry.created_at >= week_ago
            )
        ).distinct().all()
        active_user_ids = [uid[0] for uid in active_user_ids]
        if active_user_ids:
            query = query.filter(User.id.in_(active_user_ids))
    
    elif timeframe == 'month':
        month_ago = datetime.utcnow() - timedelta(days=30)
        active_user_ids = db.session.query(Review.user_id).filter(
            Review.created_at >= month_ago
        ).union(
            db.session.query(DiaryEntry.user_id).filter(
                DiaryEntry.created_at >= month_ago
            )
        ).distinct().all()
        active_user_ids = [uid[0] for uid in active_user_ids]
        if active_user_ids:
            query = query.filter(User.id.in_(active_user_ids))
    
    # Order by followers
    query = query.order_by(desc(User.followers_count), desc(User.total_reviews))
    
    # Paginate
    pagination = query.paginate(page=page, per_page=per_page, error_out=False)  # type: ignore
    
    users_data = []
    for user in pagination.items:
        user_dict = {
            'id': user.id,
            'username': user.username,
            'first_name': user.first_name,
            'last_name': user.last_name,
            'profile_picture': user.profile_picture,
            'bio': user.bio,
            'followers_count': user.followers_count or 0,
            'following_count': user.following_count or 0,
            'total_reviews': user.total_reviews or 0,
            'is_following': False
        }
        
        if current_user.is_authenticated:
            is_following = UserFollow.query.filter_by(
                follower_id=current_user.id,
                following_id=user.id,
                is_active=True
            ).first() is not None
            user_dict['is_following'] = is_following
        
        users_data.append(user_dict)
    
    return jsonify({
        'users': users_data,
        'total': pagination.total,
        'pages': pagination.pages,
        'current_page': page,
        'has_next': pagination.has_next,
        'has_prev': pagination.has_prev,
        'timeframe': timeframe
    }), 200


# ============================================================================
# SIMILAR USERS (by taste)
# ============================================================================

@user_discovery.route('/api/users/<int:user_id>/similar', methods=['GET'])
def similar_users(user_id):
    """Find users with similar taste to the specified user"""
    user = User.query.get_or_404(user_id)
    limit = request.args.get('limit', 10, type=int)
    
    # Get users who have reviewed the same movies with similar ratings
    # This is a simplified version - can be enhanced with the user_similarity table
    
    # Get movies the target user has reviewed
    user_reviews = Review.query.filter(
        Review.user_id == user_id,
        Review.is_deleted == False
    ).limit(50).all()
    
    if not user_reviews:
        return jsonify({'users': []}), 200
    
    media_ids = [r.media_id for r in user_reviews]
    
    # Find other users who reviewed the same movies
    similar_user_scores = db.session.query(
        Review.user_id,
        func.count(Review.id).label('common_movies'),
        func.avg(func.abs(Review.rating - user.total_reviews)).label('avg_rating_diff')
    ).filter(
        Review.media_id.in_(media_ids),
        Review.user_id != user_id,
        Review.is_deleted == False
    ).group_by(Review.user_id).order_by(
        desc('common_movies'),
        'avg_rating_diff'
    ).limit(limit).all()
    
    if not similar_user_scores:
        return jsonify({'users': []}), 200
    
    similar_user_ids = [uid for uid, _, _ in similar_user_scores]
    users = User.query.filter(User.id.in_(similar_user_ids)).all()
    
    # Build response
    users_data = []
    for similar_user in users:
        # Get stats for this user
        common_movies = next((count for uid, count, _ in similar_user_scores if uid == similar_user.id), 0)
        
        user_dict = {
            'id': similar_user.id,
            'username': similar_user.username,
            'first_name': similar_user.first_name,
            'last_name': similar_user.last_name,
            'profile_picture': similar_user.profile_picture,
            'bio': similar_user.bio,
            'followers_count': similar_user.followers_count or 0,
            'total_reviews': similar_user.total_reviews or 0,
            'common_movies': common_movies,
            'similarity_reason': f"{common_movies} movies in common",
            'is_following': False
        }
        
        if current_user.is_authenticated:
            is_following = UserFollow.query.filter_by(
                follower_id=current_user.id,
                following_id=similar_user.id,
                is_active=True
            ).first() is not None
            user_dict['is_following'] = is_following
        
        users_data.append(user_dict)
    
    return jsonify({
        'target_user': {
            'id': user.id,
            'username': user.username
        },
        'similar_users': users_data
    }), 200


# ============================================================================
# QUICK USER AUTOCOMPLETE
# ============================================================================

@user_discovery.route('/api/users/autocomplete', methods=['GET'])
def autocomplete_users():
    """Quick autocomplete for username search (for @mentions, etc.)"""
    query_str = request.args.get('q', '').strip()
    limit = request.args.get('limit', 10, type=int)
    
    if len(query_str) < 2:
        return jsonify({'users': []}), 200
    
    users = User.query.filter(
        User.username.ilike(f'{query_str}%'),
        User.is_active == True  # type: ignore
    ).order_by(
        User.followers_count.desc()
    ).limit(limit).all()
    
    users_data = [{
        'id': u.id,
        'username': u.username,
        'profile_picture': u.profile_picture
    } for u in users]
    
    return jsonify({'users': users_data}), 200
