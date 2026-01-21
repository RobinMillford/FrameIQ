"""
Review API Routes
Handles CRUD operations for user-generated reviews
"""
from flask import Blueprint, request, jsonify
from flask_login import login_required, current_user
from models import db, Review, ReviewLike, ReviewComment, MediaItem, User, UserFollow
from datetime import datetime
from sqlalchemy.exc import IntegrityError

reviews = Blueprint('reviews', __name__)


@reviews.route('/api/reviews', methods=['POST'])
@login_required
def create_review():
    """Create a new review"""
    try:
        data = request.get_json()
        
        # Validate required fields
        if not data.get('media_id') or not data.get('media_type') or not data.get('rating'):
            return jsonify({'error': 'Missing required fields'}), 400
        
        # Validate rating range
        rating = float(data['rating'])
        if rating < 0.5 or rating > 5.0:
            return jsonify({'error': 'Rating must be between 0.5 and 5.0'}), 400
        
        # Check if media exists, create if not
        media = MediaItem.query.filter_by(
            tmdb_id=data['media_id'],
            media_type=data['media_type']
        ).first()
        
        if not media:
            # Create media item
            media = MediaItem(
                tmdb_id=data['media_id'],
                media_type=data['media_type'],
                title=data.get('title', 'Unknown'),
                poster_path=data.get('poster_path'),
                genres=data.get('genres'),  # New field
                overview=data.get('overview'),
                release_date=datetime.strptime(data['release_date'], '%Y-%m-%d').date() if data.get('release_date') else None
            )
            db.session.add(media)
            db.session.flush()  # Get media.id
        
        # Parse watched date if provided
        watched_date = None
        if data.get('watched_date'):
            try:
                watched_date = datetime.strptime(data['watched_date'], '%Y-%m-%d').date()
            except ValueError:
                return jsonify({'error': 'Invalid date format. Use YYYY-MM-DD'}), 400
        
        # Create review
        review = Review(
            user_id=current_user.id,
            media_id=media.id,
            media_type=data['media_type'],
            content=data.get('content', '').strip() or None,
            rating=rating,
            watched_date=watched_date,
            contains_spoilers=data.get('contains_spoilers', False)
        )
        
        db.session.add(review)
        
        # Update user stats
        current_user.total_reviews = (current_user.total_reviews or 0) + 1
        current_user.total_movies_watched = (current_user.total_movies_watched or 0) + 1
        
        db.session.commit()
        
        return jsonify({
            'message': 'Review created successfully',
            'review': review.to_dict()
        }), 201
        
    except IntegrityError:
        db.session.rollback()
        return jsonify({'error': 'You have already reviewed this item'}), 409
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 500


@reviews.route('/api/reviews/<int:review_id>', methods=['GET'])
def get_review(review_id):
    """Get a specific review"""
    review = Review.query.filter_by(id=review_id, is_deleted=False).first()
    
    if not review:
        return jsonify({'error': 'Review not found'}), 404
    
    return jsonify(review.to_dict()), 200


@reviews.route('/api/reviews/<int:review_id>', methods=['PUT'])
@login_required
def update_review(review_id):
    """Update an existing review"""
    review = Review.query.filter_by(id=review_id, is_deleted=False).first()
    
    if not review:
        return jsonify({'error': 'Review not found'}), 404
    
    # Check ownership
    if review.user_id != current_user.id:
        return jsonify({'error': 'Unauthorized'}), 403
    
    try:
        data = request.get_json()
        
        # Update fields if provided
        if 'rating' in data:
            rating = float(data['rating'])
            if rating < 0.5 or rating > 5.0:
                return jsonify({'error': 'Rating must be between 0.5 and 5.0'}), 400
            review.rating = rating
        
        if 'content' in data:
            review.content = data['content'].strip() or None
        
        if 'watched_date' in data:
            if data['watched_date']:
                try:
                    review.watched_date = datetime.strptime(data['watched_date'], '%Y-%m-%d').date()
                except ValueError:
                    return jsonify({'error': 'Invalid date format. Use YYYY-MM-DD'}), 400
            else:
                review.watched_date = None
        
        if 'contains_spoilers' in data:
            review.contains_spoilers = bool(data['contains_spoilers'])
        
        review.updated_at = datetime.utcnow()
        
        db.session.commit()
        
        return jsonify({
            'message': 'Review updated successfully',
            'review': review.to_dict()
        }), 200
        
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 500


@reviews.route('/api/reviews/<int:review_id>', methods=['DELETE'])
@login_required
def delete_review(review_id):
    """Delete a review (soft delete)"""
    review = Review.query.filter_by(id=review_id, is_deleted=False).first()
    
    if not review:
        return jsonify({'error': 'Review not found'}), 404
    
    # Check ownership
    if review.user_id != current_user.id:
        return jsonify({'error': 'Unauthorized'}), 403
    
    try:
        # Soft delete
        review.is_deleted = True
        
        # Update user stats
        current_user.total_reviews = max(0, (current_user.total_reviews or 0) - 1)
        
        db.session.commit()
        
        return jsonify({'message': 'Review deleted successfully'}), 200
        
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 500


@reviews.route('/api/reviews/<int:review_id>/comments', methods=['POST'])
@login_required
def create_comment(review_id):
    """Add a comment to a review or reply to another comment"""
    review = Review.query.filter_by(id=review_id, is_deleted=False).first()
    if not review:
        return jsonify({'error': 'Review not found'}), 404
        
    try:
        data = request.get_json()
        content = data.get('content', '').strip()
        parent_id = data.get('parent_id')
        
        if not content:
            return jsonify({'error': 'Comment content is required'}), 400
            
        # If parent_id is provided, verify it exists and belongs to the same review
        if parent_id:
            parent = ReviewComment.query.get(parent_id)
            if not parent or parent.review_id != review_id:
                return jsonify({'error': 'Invalid parent comment'}), 400
        
        comment = ReviewComment(
            user_id=current_user.id,
            review_id=review_id,
            parent_id=parent_id,
            content=content
        )
        
        db.session.add(comment)
        
        # Update review comment count
        review.comments_count = (review.comments_count or 0) + 1
        
        db.session.commit()
        
        return jsonify({
            'message': 'Comment added successfully',
            'comment': comment.to_dict()
        }), 201
        
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 500


@reviews.route('/api/reviews/<int:review_id>/comments', methods=['GET'])
def get_review_comments(review_id):
    """Get all comments for a review"""
    review = Review.query.filter_by(id=review_id, is_deleted=False).first()
    if not review:
        return jsonify({'error': 'Review not found'}), 404
        
    # Get all comments for this review (not deleted)
    comments = ReviewComment.query.filter_by(
        review_id=review_id,
        is_deleted=False
    ).order_by(ReviewComment.created_at.asc()).all()
    
    return jsonify({
        'comments': [c.to_dict() for c in comments],
        'total': len(comments)
    }), 200


@reviews.route('/api/media/<media_type>/<int:tmdb_id>/reviews', methods=['GET'])
def get_media_reviews(media_type, tmdb_id):
    """Get all reviews for a specific movie/TV show"""
    # Find media item
    media = MediaItem.query.filter_by(
        tmdb_id=tmdb_id,
        media_type=media_type
    ).first()
    
    if not media:
        return jsonify({'reviews': []}), 200
    
    # Get pagination parameters
    page = request.args.get('page', 1, type=int)
    per_page = request.args.get('per_page', 10, type=int)
    
    # Query reviews
    reviews_query = Review.query.filter_by(
        media_id=media.id,
        is_deleted=False
    ).order_by(Review.created_at.desc())
    
    # Paginate
    pagination = reviews_query.paginate(page=page, per_page=per_page, error_out=False)
    
    reviews_data = [review.to_dict() for review in pagination.items]
    
    # Check which reviews current user has liked and authors they follow (if logged in)
    if current_user.is_authenticated:
        liked_review_ids = {like.review_id for like in ReviewLike.query.filter_by(user_id=current_user.id).all()}
        followed_user_ids = {follow.following_id for follow in UserFollow.query.filter_by(follower_id=current_user.id, is_active=True).all()}
        for review_data in reviews_data:
            review_data['is_liked_by_user'] = review_data['id'] in liked_review_ids
            # Pass user id of the author to check against followed_user_ids
            author_id = review_data['user']['id']
            review_data['is_following_author'] = author_id in followed_user_ids
            # Also check if it's the current user themselves
            review_data['is_author_self'] = author_id == current_user.id
    
    return jsonify({
        'reviews': reviews_data,
        'total': pagination.total,
        'pages': pagination.pages,
        'current_page': page,
        'has_next': pagination.has_next,
        'has_prev': pagination.has_prev
    }), 200


@reviews.route('/api/users/<int:user_id>/reviews', methods=['GET'])
def get_user_reviews(user_id):
    """Get all reviews by a specific user"""
    user = User.query.get(user_id)
    
    if not user:
        return jsonify({'error': 'User not found'}), 404
    
    # Get pagination parameters
    page = request.args.get('page', 1, type=int)
    per_page = request.args.get('per_page', 20, type=int)
    
    # Query reviews
    reviews_query = Review.query.filter_by(
        user_id=user_id,
        is_deleted=False
    ).order_by(Review.created_at.desc())
    
    # Paginate
    pagination = reviews_query.paginate(page=page, per_page=per_page, error_out=False)
    
    reviews_data = [review.to_dict() for review in pagination.items]
    
    return jsonify({
        'reviews': reviews_data,
        'total': pagination.total,
        'pages': pagination.pages,
        'current_page': page
    }), 200
