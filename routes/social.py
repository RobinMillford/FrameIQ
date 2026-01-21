"""
Social API Routes
Handles user following/follower relationships
"""
from flask import Blueprint, request, jsonify
from flask_login import login_required, current_user
from models import db, User, UserFollow, Review
from sqlalchemy.exc import IntegrityError
from sqlalchemy import or_, and_

social = Blueprint('social', __name__)


@social.route('/api/users/<int:user_id>/follow', methods=['POST'])
@login_required
def toggle_follow(user_id):
    """Follow or unfollow a user"""
    # Prevent self-following
    if user_id == current_user.id:
        return jsonify({'error': 'You cannot follow yourself'}), 400
    
    # Check if target user exists
    target_user = User.query.get(user_id)
    if not target_user:
        return jsonify({'error': 'User not found'}), 404
    
    try:
        # Check if already following
        existing_follow = UserFollow.query.filter_by(
            follower_id=current_user.id,
            following_id=user_id,
            is_active=True
        ).first()
        
        if existing_follow:
            # Unfollow (soft delete)
            existing_follow.is_active = False
            
            # Update counts
            current_user.following_count = max(0, (current_user.following_count or 0) - 1)
            target_user.followers_count = max(0, (target_user.followers_count or 0) - 1)
            
            action = 'unfollowed'
            is_following = False
        else:
            # Check if there's an inactive follow (previously unfollowed)
            inactive_follow = UserFollow.query.filter_by(
                follower_id=current_user.id,
                following_id=user_id,
                is_active=False
            ).first()
            
            if inactive_follow:
                # Reactivate the follow
                inactive_follow.is_active = True
            else:
                # Create new follow
                follow = UserFollow(
                    follower_id=current_user.id,
                    following_id=user_id
                )
                db.session.add(follow)
            
            # Update counts
            current_user.following_count = (current_user.following_count or 0) + 1
            target_user.followers_count = (target_user.followers_count or 0) + 1
            
            action = 'followed'
            is_following = True
        
        db.session.commit()
        
        return jsonify({
            'message': f'Successfully {action} {target_user.username}',
            'is_following': is_following,
            'followers_count': target_user.followers_count,
            'following_count': current_user.following_count
        }), 200
        
    except IntegrityError as e:
        db.session.rollback()
        return jsonify({'error': 'Database error occurred'}), 500
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 500


@social.route('/api/users/<int:user_id>/followers', methods=['GET'])
def get_followers(user_id):
    """Get list of users following the target user"""
    # Check if user exists
    user = User.query.get(user_id)
    if not user:
        return jsonify({'error': 'User not found'}), 404
    
    # Get pagination parameters
    page = request.args.get('page', 1, type=int)
    per_page = request.args.get('per_page', 20, type=int)
    
    # Query followers (users who follow this user)
    followers_query = UserFollow.query.filter_by(
        following_id=user_id,
        is_active=True
    ).order_by(UserFollow.created_at.desc())
    
    # Paginate
    pagination = followers_query.paginate(page=page, per_page=per_page, error_out=False)
    
    # Build follower list with user details
    followers_data = []
    for follow in pagination.items:
        follower = follow.follower_user
        follower_data = {
            'id': follower.id,
            'username': follower.username,
            'profile_picture': follower.profile_picture,
            'bio': follower.bio,
            'followers_count': follower.followers_count or 0,
            'following_count': follower.following_count or 0,
            'total_reviews': follower.total_reviews or 0,
            'followed_at': follow.created_at.isoformat()
        }
        
        # Check if current user follows this follower
        if current_user.is_authenticated:
            is_following = UserFollow.query.filter_by(
                follower_id=current_user.id,
                following_id=follower.id,
                is_active=True
            ).first() is not None
            follower_data['is_following'] = is_following
        
        followers_data.append(follower_data)
    
    return jsonify({
        'followers': followers_data,
        'total': pagination.total,
        'pages': pagination.pages,
        'current_page': page,
        'has_next': pagination.has_next,
        'has_prev': pagination.has_prev,
        'current_user_id': current_user.id if current_user.is_authenticated else None
    }), 200


@social.route('/api/users/<int:user_id>/following', methods=['GET'])
def get_following(user_id):
    """Get list of users that the target user is following"""
    # Check if user exists
    user = User.query.get(user_id)
    if not user:
        return jsonify({'error': 'User not found'}), 404
    
    # Get pagination parameters
    page = request.args.get('page', 1, type=int)
    per_page = request.args.get('per_page', 20, type=int)
    
    # Query following (users this user follows)
    following_query = UserFollow.query.filter_by(
        follower_id=user_id,
        is_active=True
    ).order_by(UserFollow.created_at.desc())
    
    # Paginate
    pagination = following_query.paginate(page=page, per_page=per_page, error_out=False)
    
    # Build following list with user details
    following_data = []
    for follow in pagination.items:
        following_user = follow.following_user
        following_user_data = {
            'id': following_user.id,
            'username': following_user.username,
            'profile_picture': following_user.profile_picture,
            'bio': following_user.bio,
            'followers_count': following_user.followers_count or 0,
            'following_count': following_user.following_count or 0,
            'total_reviews': following_user.total_reviews or 0,
            'followed_at': follow.created_at.isoformat()
        }
        
        # Check if current user follows this user
        if current_user.is_authenticated:
            is_following = UserFollow.query.filter_by(
                follower_id=current_user.id,
                following_id=following_user.id,
                is_active=True
            ).first() is not None
            following_user_data['is_following'] = is_following
        
        following_data.append(following_user_data)
    
    return jsonify({
        'following': following_data,
        'total': pagination.total,
        'pages': pagination.pages,
        'current_page': page,
        'has_next': pagination.has_next,
        'has_prev': pagination.has_prev,
        'current_user_id': current_user.id if current_user.is_authenticated else None
    }), 200


@social.route('/api/users/<int:user_id>/follow-status', methods=['GET'])
@login_required
def get_follow_status(user_id):
    """Check follow status between current user and target user"""
    # Check if target user exists
    target_user = User.query.get(user_id)
    if not target_user:
        return jsonify({'error': 'User not found'}), 404
    
    # Check if current user follows target user
    is_following = UserFollow.query.filter_by(
        follower_id=current_user.id,
        following_id=user_id,
        is_active=True
    ).first() is not None
    
    # Check if target user follows current user
    is_followed_by = UserFollow.query.filter_by(
        follower_id=user_id,
        following_id=current_user.id,
        is_active=True
    ).first() is not None
    
    return jsonify({
        'is_following': is_following,
        'is_followed_by': is_followed_by,
        'is_mutual': is_following and is_followed_by
    }), 200


@social.route('/api/social/feed', methods=['GET'])
@login_required
def get_activity_feed():
    """Get recent reviews from users the current user follows"""
    page = request.args.get('page', 1, type=int)
    per_page = request.args.get('per_page', 20, type=int)
    
    # Query: Get reviews from users the current user follows
    feed_query = db.session.query(Review).join(
        UserFollow, Review.user_id == UserFollow.following_id
    ).filter(
        UserFollow.follower_id == current_user.id,
        UserFollow.is_active == True,
        Review.is_deleted == False
    ).order_by(Review.created_at.desc())
    
    # Paginate
    pagination = feed_query.paginate(page=page, per_page=per_page, error_out=False)
    
    return jsonify({
        'reviews': [review.to_dict() for review in pagination.items],
        'total': pagination.total,
        'pages': pagination.pages,
        'current_page': page,
        'has_next': pagination.has_next,
        'has_prev': pagination.has_prev
    }), 200


@social.route('/api/social/global-feed', methods=['GET'])
def get_global_feed():
    """Get recent reviews from all users"""
    page = request.args.get('page', 1, type=int)
    per_page = request.args.get('per_page', 20, type=int)
    
    # Query: Get all reviews
    feed_query = Review.query.filter_by(is_deleted=False).order_by(Review.created_at.desc())
    
    # Paginate
    pagination = feed_query.paginate(page=page, per_page=per_page, error_out=False)
    
    return jsonify({
        'reviews': [review.to_dict() for review in pagination.items],
        'total': pagination.total,
        'pages': pagination.pages,
        'current_page': page,
        'has_next': pagination.has_next,
        'has_prev': pagination.has_prev
    }), 200
