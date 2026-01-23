"""
Week 3: Reviews System Enhancement API Routes
Adds helpful votes and enhanced discovery features
"""
from flask import Blueprint, request, jsonify, render_template
from flask_login import login_required, current_user
from models import db, Review, ReviewLike, ReviewHelpful, ReviewComment, User, UserFollow, MediaItem
from datetime import datetime, date
from sqlalchemy import desc, func, and_

reviews_enhanced_bp = Blueprint('reviews_enhanced', __name__)


# ============================================================================
# REVIEW FEED PAGES (HTML)
# ============================================================================

@reviews_enhanced_bp.route('/reviews/feed')
def review_feed_page():
    """Render the review feed page"""
    return render_template('review_feed.html')


@reviews_enhanced_bp.route('/reviews/popular')
def review_popular_page():
    """Render the popular reviews page"""
    return render_template('review_popular.html')


# ============================================================================
# REVIEW CRUD
# ============================================================================

@reviews_enhanced_bp.route('/api/reviews', methods=['POST'])
@login_required
def create_review():
    """Create a new review"""
    data = request.get_json()
    
    # Validate required fields
    media_id = data.get('media_id')
    media_type = data.get('media_type')
    rating = data.get('rating')
    
    if not all([media_id, media_type, rating]):
        return jsonify({'error': 'media_id, media_type, and rating are required'}), 400
    
    if media_type not in ['movie', 'tv']:
        return jsonify({'error': 'media_type must be "movie" or "tv"'}), 400
    
    # Validate rating (0.5 to 5.0 in 0.5 increments)
    try:
        rating = float(rating)
        if rating < 0.5 or rating > 5.0 or (rating * 2) % 1 != 0:
            return jsonify({'error': 'rating must be between 0.5 and 5.0 in 0.5 increments'}), 400
    except (ValueError, TypeError):
        return jsonify({'error': 'Invalid rating format'}), 400
    
    # Check if user already has a review for this media
    existing = Review.query.filter_by(
        user_id=current_user.id,
        media_id=media_id,
        media_type=media_type,
        is_deleted=False
    ).first()
    
    if existing:
        return jsonify({'error': 'You already have a review for this item'}), 400
    
    try:
        # Parse watched_date if provided
        watched_date = None
        if data.get('watched_date'):
            try:
                watched_date = datetime.strptime(data['watched_date'], '%Y-%m-%d').date()
            except ValueError:
                return jsonify({'error': 'Invalid date format. Use YYYY-MM-DD'}), 400
        
        review = Review(
            user_id=current_user.id,
            media_id=media_id,
            media_type=media_type,
            rating=rating,
            title=data.get('title', '').strip() or None,
            content=data.get('content', '').strip() or None,
            has_spoilers=data.get('has_spoilers', False),
            watched_date=watched_date,
            rewatch=data.get('rewatch', False)
        )
        
        db.session.add(review)
        db.session.commit()
        
        return jsonify({
            'message': 'Review created successfully',
            'review': review.to_dict()
        }), 201
        
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 500


@reviews_enhanced_bp.route('/api/reviews/<int:review_id>', methods=['GET'])
def get_review(review_id):
    """Get a single review with all details"""
    review = Review.query.filter_by(id=review_id, is_deleted=False).first_or_404()
    
    # Get user's interaction status if authenticated
    user_liked = False
    user_helpful_vote = None
    
    if current_user.is_authenticated:
        user_liked = ReviewLike.query.filter_by(
            user_id=current_user.id,
            review_id=review_id
        ).first() is not None
        
        helpful_vote = ReviewHelpful.query.filter_by(
            user_id=current_user.id,
            review_id=review_id
        ).first()
        if helpful_vote:
            user_helpful_vote = helpful_vote.is_helpful
    
    review_data = review.to_dict()
    review_data['user_liked'] = user_liked
    review_data['user_helpful_vote'] = user_helpful_vote
    
    # Get replies (comments on review)
    replies = ReviewComment.query.filter_by(review_id=review_id, is_deleted=False, parent_id=None).all()
    review_data['replies'] = [reply.to_dict() for reply in replies]
    
    return jsonify(review_data), 200


@reviews_enhanced_bp.route('/api/reviews/<int:review_id>', methods=['PUT'])
@login_required
def update_review(review_id):
    """Update an existing review"""
    review = Review.query.filter_by(id=review_id, is_deleted=False).first_or_404()
    
    # Check ownership
    if review.user_id != current_user.id:
        return jsonify({'error': 'You can only edit your own reviews'}), 403
    
    data = request.get_json()
    
    try:
        # Update rating if provided
        if 'rating' in data:
            rating = float(data['rating'])
            if rating < 0.5 or rating > 5.0 or (rating * 2) % 1 != 0:
                return jsonify({'error': 'rating must be between 0.5 and 5.0 in 0.5 increments'}), 400
            review.rating = rating
        
        # Update other fields
        if 'title' in data:
            review.title = data['title'].strip() or None
        if 'content' in data:
            review.content = data['content'].strip() or None
        if 'has_spoilers' in data:
            review.has_spoilers = data['has_spoilers']
        if 'rewatch' in data:
            review.rewatch = data['rewatch']
        
        # Update watched_date if provided
        if 'watched_date' in data:
            if data['watched_date']:
                try:
                    review.watched_date = datetime.strptime(data['watched_date'], '%Y-%m-%d').date()
                except ValueError:
                    return jsonify({'error': 'Invalid date format. Use YYYY-MM-DD'}), 400
            else:
                review.watched_date = None
        
        review.updated_at = datetime.utcnow()
        db.session.commit()
        
        return jsonify({
            'message': 'Review updated successfully',
            'review': review.to_dict()
        }), 200
        
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 500


@reviews_enhanced_bp.route('/api/reviews/<int:review_id>', methods=['DELETE'])
@login_required
def delete_review(review_id):
    """Delete a review (soft delete)"""
    review = Review.query.filter_by(id=review_id, is_deleted=False).first_or_404()
    
    # Check ownership
    if review.user_id != current_user.id:
        return jsonify({'error': 'You can only delete your own reviews'}), 403
    
    try:
        review.is_deleted = True
        db.session.commit()
        
        return jsonify({'message': 'Review deleted successfully'}), 200
        
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 500


# ============================================================================
# MEDIA REVIEWS
# ============================================================================

@reviews_enhanced_bp.route('/api/media/<int:media_id>/reviews', methods=['GET'])
def get_media_reviews(media_id):
    """Get all reviews for a specific movie/TV show (using TMDB ID)"""
    media_type = request.args.get('media_type', 'movie')
    page = request.args.get('page', 1, type=int)
    per_page = request.args.get('per_page', 10, type=int)
    sort_by = request.args.get('sort', 'recent')  # recent, popular, rating_high, rating_low, all
    
    if media_type not in ['movie', 'tv']:
        return jsonify({'error': 'media_type must be "movie" or "tv"'}), 400
    
    # Find the MediaItem by TMDB ID
    media_item = MediaItem.query.filter_by(tmdb_id=media_id, media_type=media_type).first()
    
    if not media_item:
        # No reviews yet if media item doesn't exist
        return jsonify({
            'reviews': [],
            'total': 0,
            'average_rating': 0,
            'has_next': False,
            'has_prev': False
        }), 200
    
    # Base query using internal media_item.id
    query = Review.query.filter_by(
        media_id=media_item.id,
        media_type=media_type,
        is_deleted=False
    )
    
    # Apply sorting
    if sort_by == 'popular':
        query = query.order_by(desc(Review.likes_count))
    elif sort_by == 'rating_high':
        query = query.order_by(desc(Review.rating), desc(Review.created_at))
    elif sort_by == 'rating_low':
        query = query.order_by(Review.rating.asc(), desc(Review.created_at))
    elif sort_by == 'all':
        query = query.order_by(desc(Review.created_at))  # Show all, sorted by recent
    else:  # recent (default)
        query = query.order_by(desc(Review.created_at))
    
    # Paginate
    pagination = query.paginate(page=page, per_page=per_page, error_out=False)
    
    # Calculate average rating using internal media_item.id
    avg_rating = db.session.query(func.avg(Review.rating)).filter_by(
        media_id=media_item.id,
        media_type=media_type,
        is_deleted=False
    ).scalar()
    
    return jsonify({
        'reviews': [review.to_dict() for review in pagination.items],
        'total': pagination.total,
        'pages': pagination.pages,
        'current_page': page,
        'has_next': pagination.has_next,
        'has_prev': pagination.has_prev,
        'average_rating': float(avg_rating) if avg_rating else None
    }), 200


# ============================================================================
# REVIEW DISCOVERY
# ============================================================================

@reviews_enhanced_bp.route('/api/reviews/feed', methods=['GET'])
def review_feed():
    """Get latest reviews feed (all or friends only)"""
    page = request.args.get('page', 1, type=int)
    per_page = request.args.get('per_page', 20, type=int)
    friends_only = request.args.get('friends_only', 'false').lower() == 'true'
    
    query = Review.query.filter_by(is_deleted=False)
    
    # Filter by friends if authenticated and requested
    if friends_only and current_user.is_authenticated:
        # Get IDs of users current user follows
        following_ids = db.session.query(UserFollow.following_id).filter_by(
            follower_id=current_user.id,
            is_active=True
        ).all()
        following_ids = [fid[0] for fid in following_ids]
        
        if following_ids:
            query = query.filter(Review.user_id.in_(following_ids))
        else:
            # No friends, return empty
            return jsonify({
                'reviews': [],
                'total': 0,
                'pages': 0,
                'current_page': page,
                'has_next': False,
                'has_prev': False
            }), 200
    
    # Order by most recent
    query = query.order_by(desc(Review.created_at))
    
    # Paginate
    pagination = query.paginate(page=page, per_page=per_page, error_out=False)
    
    return jsonify({
        'reviews': [review.to_dict() for review in pagination.items],
        'total': pagination.total,
        'pages': pagination.pages,
        'current_page': page,
        'has_next': pagination.has_next,
        'has_prev': pagination.has_prev
    }), 200


@reviews_enhanced_bp.route('/api/reviews/popular', methods=['GET'])
def popular_reviews():
    """Get popular reviews (most liked)"""
    page = request.args.get('page', 1, type=int)
    per_page = request.args.get('per_page', 20, type=int)
    timeframe = request.args.get('timeframe', 'week')  # week, month, year, all
    
    query = Review.query.filter_by(is_deleted=False)
    
    # Filter by timeframe
    if timeframe == 'week':
        from datetime import timedelta
        week_ago = datetime.utcnow() - timedelta(days=7)
        query = query.filter(Review.created_at >= week_ago)
    elif timeframe == 'month':
        from datetime import timedelta
        month_ago = datetime.utcnow() - timedelta(days=30)
        query = query.filter(Review.created_at >= month_ago)
    elif timeframe == 'year':
        from datetime import timedelta
        year_ago = datetime.utcnow() - timedelta(days=365)
        query = query.filter(Review.created_at >= year_ago)
    
    # Order by popularity (likes + helpful votes)
    query = query.order_by(desc(Review.likes_count + Review.helpful_count))
    
    # Paginate
    pagination = query.paginate(page=page, per_page=per_page, error_out=False)
    
    return jsonify({
        'reviews': [review.to_dict() for review in pagination.items],
        'total': pagination.total,
        'pages': pagination.pages,
        'current_page': page,
        'has_next': pagination.has_next,
        'has_prev': pagination.has_prev,
        'timeframe': timeframe
    }), 200


@reviews_enhanced_bp.route('/api/users/<int:user_id>/reviews', methods=['GET'])
def user_reviews(user_id):
    """Get all reviews by a specific user"""
    user = User.query.get_or_404(user_id)
    
    page = request.args.get('page', 1, type=int)
    per_page = request.args.get('per_page', 20, type=int)
    
    query = Review.query.filter_by(
        user_id=user_id,
        is_deleted=False
    ).order_by(desc(Review.created_at))
    
    # Paginate
    pagination = query.paginate(page=page, per_page=per_page, error_out=False)
    
    return jsonify({
        'user': {
            'id': user.id,
            'username': user.username,
            'profile_picture': user.profile_picture
        },
        'reviews': [review.to_dict(include_user=False) for review in pagination.items],
        'total': pagination.total,
        'pages': pagination.pages,
        'current_page': page,
        'has_next': pagination.has_next,
        'has_prev': pagination.has_prev
    }), 200


# ============================================================================
# REVIEW INTERACTIONS
# ============================================================================

@reviews_enhanced_bp.route('/api/reviews/<int:review_id>/like', methods=['POST'])
@login_required
def like_review(review_id):
    """Like a review"""
    review = Review.query.filter_by(id=review_id, is_deleted=False).first_or_404()
    
    # Check if already liked
    existing = ReviewLike.query.filter_by(
        user_id=current_user.id,
        review_id=review_id
    ).first()
    
    if existing:
        return jsonify({'error': 'You already liked this review'}), 400
    
    try:
        like = ReviewLike(
            user_id=current_user.id,
            review_id=review_id
        )
        db.session.add(like)
        
        # Update like count
        review.likes_count += 1
        
        db.session.commit()
        
        return jsonify({
            'message': 'Review liked',
            'like_count': review.likes_count
        }), 201
        
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 500


@reviews_enhanced_bp.route('/api/reviews/<int:review_id>/like', methods=['DELETE'])
@login_required
def unlike_review(review_id):
    """Unlike a review"""
    review = Review.query.filter_by(id=review_id, is_deleted=False).first_or_404()
    
    like = ReviewLike.query.filter_by(
        user_id=current_user.id,
        review_id=review_id
    ).first_or_404()
    
    try:
        db.session.delete(like)
        
        # Update like count
        if review.likes_count > 0:
            review.likes_count -= 1
        
        db.session.commit()
        
        return jsonify({
            'message': 'Review unliked',
            'like_count': review.likes_count
        }), 200
        
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 500


@reviews_enhanced_bp.route('/api/reviews/<int:review_id>/helpful', methods=['POST'])
@login_required
def mark_helpful(review_id):
    """Mark a review as helpful or not helpful"""
    review = Review.query.filter_by(id=review_id, is_deleted=False).first_or_404()
    
    data = request.get_json()
    is_helpful = data.get('is_helpful')
    
    if is_helpful is None:
        return jsonify({'error': 'is_helpful (true/false) is required'}), 400
    
    # Check if user already voted
    existing = ReviewHelpful.query.filter_by(
        user_id=current_user.id,
        review_id=review_id
    ).first()
    
    try:
        if existing:
            # Update existing vote
            old_vote = existing.is_helpful
            existing.is_helpful = is_helpful
            
            # Update counts
            if old_vote != is_helpful:
                if old_vote:
                    review.helpful_count -= 1
                    review.not_helpful_count += 1
                else:
                    review.not_helpful_count -= 1
                    review.helpful_count += 1
        else:
            # Create new vote
            vote = ReviewHelpful(
                user_id=current_user.id,
                review_id=review_id,
                is_helpful=is_helpful
            )
            db.session.add(vote)
            
            # Update counts
            if is_helpful:
                review.helpful_count += 1
            else:
                review.not_helpful_count += 1
        
        db.session.commit()
        
        return jsonify({
            'message': 'Vote recorded',
            'helpful_count': review.helpful_count,
            'not_helpful_count': review.not_helpful_count
        }), 200
        
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 500


@reviews_enhanced_bp.route('/api/reviews/<int:review_id>/replies', methods=['GET'])
def get_review_replies(review_id):
    """Get all replies for a review"""
    review = Review.query.filter_by(id=review_id, is_deleted=False).first_or_404()
    
    replies = ReviewComment.query.filter_by(
        review_id=review_id,
        is_deleted=False,
        parent_id=None
    ).order_by(ReviewComment.created_at.asc()).all()
    
    return jsonify({
        'replies': [reply.to_dict() for reply in replies],
        'count': len(replies)
    }), 200


@reviews_enhanced_bp.route('/api/reviews/<int:review_id>/replies', methods=['POST'])
@login_required
def create_review_reply(review_id):
    """Reply to a review"""
    review = Review.query.filter_by(id=review_id, is_deleted=False).first_or_404()
    
    data = request.get_json()
    content = data.get('content', '').strip()
    
    if not content:
        return jsonify({'error': 'content is required'}), 400
    
    if len(content) > 5000:
        return jsonify({'error': 'Reply must be 5000 characters or less'}), 400
    
    try:
        reply = ReviewComment(
            review_id=review_id,
            user_id=current_user.id,
            content=content
        )
        db.session.add(reply)
        
        # Update comment count
        review.comments_count += 1
        
        db.session.commit()
        
        return jsonify({
            'message': 'Reply posted',
            'reply': reply.to_dict()
        }), 201
        
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 500


@reviews_enhanced_bp.route('/api/reviews/replies/<int:reply_id>', methods=['DELETE'])
@login_required
def delete_review_reply(reply_id):
    """Delete a reply"""
    reply = ReviewComment.query.filter_by(id=reply_id, is_deleted=False).first_or_404()
    
    # Check ownership
    if reply.user_id != current_user.id:
        return jsonify({'error': 'You can only delete your own replies'}), 403
    
    try:
        reply.is_deleted = True
        
        # Update comment count
        review = Review.query.get(reply.review_id)
        if review and review.comments_count > 0:
            review.comments_count -= 1
        
        db.session.commit()
        
        return jsonify({'message': 'Reply deleted'}), 200
        
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 500
