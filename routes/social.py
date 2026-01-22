"""
Social API Routes
Handles user following/follower relationships
"""
from flask import Blueprint, request, jsonify, render_template
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


@social.route('/feed')
def feed():
    """Render the social activity feed page"""
    return render_template('feed.html')


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

@social.route('/api/social/suggested-follows', methods=['GET'])
@login_required
def get_suggested_follows():
    """Get user recommendations based on shared movie tastes"""
    limit = request.args.get('limit', 5, type=int)
    
    # Get all media IDs the current user has interacted with
    watchlist_ids = {item.id for item in current_user.watchlist}
    wishlist_ids = {item.id for item in current_user.wishlist}
    viewed_ids = {item.id for item in current_user.viewed_media}
    
    all_current_user_media = watchlist_ids | wishlist_ids | viewed_ids
    
    # Get IDs of users already being followed
    following_ids = {f.following_id for f in current_user.following if f.is_active}
    following_ids.add(current_user.id)  # Exclude self
    
    # Taste matching logic:
    # 1. Find users who have these same movies in their lists
    # we'll look across watchlist, wishlist, and viewed for other users
    from models import user_watchlist, user_wishlist, user_viewed
    from sqlalchemy import union_all, select, literal_column
    
    # Create a subquery for all media interactions by all users
    interactions = union_all(
        select(user_watchlist.c.user_id, user_watchlist.c.media_id),
        select(user_wishlist.c.user_id, user_wishlist.c.media_id),
        select(user_viewed.c.user_id, user_viewed.c.media_id)
    ).alias('all_interactions')
    
    from sqlalchemy import text
    
    # Query for user IDs with shared media items
    shared_counts_query = db.session.query(
        User.id, 
        db.func.count(interactions.c.media_id).label('shared_count')
    ).join(
        interactions, User.id == interactions.c.user_id
    ).filter(
        interactions.c.media_id.in_(all_current_user_media) if all_current_user_media else db.false(),
        User.id.notin_(following_ids)
    ).group_by(User.id).order_by(db.desc('shared_count')).limit(limit)
    
    # Store ID and count pairs
    shared_media_results = shared_counts_query.all()
    user_ids = [r[0] for r in shared_media_results]
    counts_map = {r[0]: r[1] for r in shared_media_results}
    
    # Fetch full user objects for these IDs
    matched_users = []
    if user_ids:
        users_lookup = {u.id: u for u in User.query.filter(User.id.in_(user_ids)).all()}
        # Maintain order from shared_media_results
        matched_users = [(users_lookup[uid], counts_map[uid]) for uid in user_ids if uid in users_lookup]
    
    # If not enough results based on shared media, fill with active community members
    suggestions = []
    for u, count in matched_users:
        suggestions.append({
            'id': u.id,
            'username': u.username,
            'profile_picture': u.profile_picture,
            'bio': u.bio,
            'followers_count': u.followers_count or 0,
            'total_reviews': u.total_reviews or 0,
            'shared_count': count,
            'reason': f'Shared {count} movie{"s" if count > 1 else ""} with you'
        })
        
    if len(suggestions) < limit:
        already_suggested = {u.id for u, count in matched_users}
        remaining_limit = limit - len(suggestions)
        
        fallback_users = User.query.filter(
            User.id.notin_(following_ids),
            User.id.notin_(already_suggested)
        ).order_by(User.total_reviews.desc()).limit(remaining_limit).all()
        
        for u in fallback_users:
            suggestions.append({
                'id': u.id,
                'username': u.username,
                'profile_picture': u.profile_picture,
                'bio': u.bio,
                'followers_count': u.followers_count or 0,
                'total_reviews': u.total_reviews or 0,
                'shared_count': 0,
                'reason': 'Active in the community'
            })
            
    return jsonify({
        'suggestions': suggestions,
        'count': len(suggestions)
    }), 200
