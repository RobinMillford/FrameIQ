# File: routes/media_comments.py
from flask import Blueprint, request, jsonify
from flask_login import login_required, current_user
from models import db, MediaComment
from sqlalchemy import desc

media_comments_bp = Blueprint('media_comments', __name__)


@media_comments_bp.route('/api/media/<int:media_id>/comments', methods=['GET'])
def get_media_comments(media_id):
    """Get all comments for a media item"""
    media_type = request.args.get('media_type', 'movie')
    
    comments = MediaComment.query.filter_by(
        media_id=media_id,
        media_type=media_type,
        is_deleted=False
    ).order_by(MediaComment.created_at).all()
    
    return jsonify({
        'comments': [comment.to_dict() for comment in comments],
        'count': len(comments)
    }), 200


@media_comments_bp.route('/api/media/<int:media_id>/comments', methods=['POST'])
@login_required
def add_media_comment(media_id):
    """Add a comment to a media item"""
    data = request.get_json()
    
    if not data or 'content' not in data or 'media_type' not in data:
        return jsonify({'error': 'content and media_type are required'}), 400
    
    content = data['content'].strip()
    media_type = data['media_type']
    
    if not content:
        return jsonify({'error': 'Comment cannot be empty'}), 400
    
    if len(content) > 5000:
        return jsonify({'error': 'Comment too long (max 5000 characters)'}), 400
    
    # Create comment
    comment = MediaComment(
        user_id=current_user.id,
        media_id=media_id,
        media_type=media_type,
        content=content
    )
    db.session.add(comment)
    db.session.commit()
    
    return jsonify({
        'message': 'Comment added successfully',
        'comment': comment.to_dict()
    }), 201


@media_comments_bp.route('/api/media/comments/<int:comment_id>', methods=['PUT'])
@login_required
def update_media_comment(comment_id):
    """Update a comment"""
    comment = MediaComment.query.get(comment_id)
    
    if not comment:
        return jsonify({'error': 'Comment not found'}), 404
    
    if comment.user_id != current_user.id:
        return jsonify({'error': 'Unauthorized'}), 403
    
    data = request.get_json()
    if not data or 'content' not in data:
        return jsonify({'error': 'content is required'}), 400
    
    content = data['content'].strip()
    
    if not content:
        return jsonify({'error': 'Comment cannot be empty'}), 400
    
    if len(content) > 5000:
        return jsonify({'error': 'Comment too long (max 5000 characters)'}), 400
    
    comment.content = content
    db.session.commit()
    
    return jsonify({
        'message': 'Comment updated successfully',
        'comment': comment.to_dict()
    }), 200


@media_comments_bp.route('/api/media/comments/<int:comment_id>', methods=['DELETE'])
@login_required
def delete_media_comment(comment_id):
    """Delete (soft delete) a comment"""
    comment = MediaComment.query.get(comment_id)
    
    if not comment:
        return jsonify({'error': 'Comment not found'}), 404
    
    if comment.user_id != current_user.id:
        return jsonify({'error': 'Unauthorized'}), 403
    
    comment.is_deleted = True
    db.session.commit()
    
    return jsonify({'message': 'Comment deleted successfully'}), 200
