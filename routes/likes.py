# File: routes/likes.py
from flask import Blueprint, request, jsonify
from flask_login import login_required, current_user
from models import db, MediaLike
from sqlalchemy import desc

likes_bp = Blueprint('likes', __name__)


@likes_bp.route('/api/media/<int:media_id>/like', methods=['POST'])
@login_required
def like_media(media_id):
    """Like (heart) a movie/TV show"""
    data = request.get_json()
    
    if not data or 'media_type' not in data:
        return jsonify({'error': 'media_type is required'}), 400
    
    media_type = data['media_type']
    
    # Check if already liked
    existing = MediaLike.query.filter_by(
        user_id=current_user.id,
        media_id=media_id,
        media_type=media_type
    ).first()
    
    if existing:
        return jsonify({'error': 'Already liked'}), 400
    
    # Create like
    like = MediaLike(
        user_id=current_user.id,
        media_id=media_id,
        media_type=media_type
    )
    db.session.add(like)
    db.session.commit()
    
    return jsonify({
        'message': 'Liked successfully',
        'like': like.to_dict()
    }), 201


@likes_bp.route('/api/media/<int:media_id>/like', methods=['DELETE'])
@login_required
def unlike_media(media_id):
    """Unlike (remove heart) from a movie/TV show"""
    media_type = request.args.get('media_type', 'movie')
    
    # Find the like
    like = MediaLike.query.filter_by(
        user_id=current_user.id,
        media_id=media_id,
        media_type=media_type
    ).first()
    
    if not like:
        return jsonify({'error': 'Not liked'}), 404
    
    db.session.delete(like)
    db.session.commit()
    
    return jsonify({'message': 'Unliked successfully'}), 200


@likes_bp.route('/api/media/<int:media_id>/likes', methods=['GET'])
def get_media_likes(media_id):
    """Get all likes for a media item"""
    media_type = request.args.get('media_type', 'movie')
    
    likes = MediaLike.query.filter_by(
        media_id=media_id,
        media_type=media_type
    ).order_by(desc(MediaLike.created_at)).all()
    
    return jsonify({
        'likes': [like.to_dict() for like in likes],
        'count': len(likes)
    }), 200


@likes_bp.route('/api/media/<int:media_id>/likes/check', methods=['GET'])
@login_required
def check_if_liked(media_id):
    """Check if current user has liked this media"""
    media_type = request.args.get('media_type', 'movie')
    
    liked = MediaLike.query.filter_by(
        user_id=current_user.id,
        media_id=media_id,
        media_type=media_type
    ).first() is not None
    
    return jsonify({'liked': liked}), 200


@likes_bp.route('/api/users/<int:user_id>/likes', methods=['GET'])
def get_user_likes(user_id):
    """Get all media liked by a user"""
    media_type = request.args.get('media_type')  # Optional filter
    
    query = MediaLike.query.filter_by(user_id=user_id)
    
    if media_type:
        query = query.filter_by(media_type=media_type)
    
    likes = query.order_by(desc(MediaLike.created_at)).all()
    
    return jsonify({
        'likes': [like.to_dict() for like in likes],
        'count': len(likes)
    }), 200
