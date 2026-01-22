# File: routes/tags.py
from flask import Blueprint, request, jsonify, render_template
from flask_login import login_required, current_user
from models import db, Tag, UserMediaTag
from sqlalchemy import func, desc

tags_bp = Blueprint('tags', __name__)


@tags_bp.route('/test-tags')
def test_tags_page():
    """Test page for tags API"""
    return render_template('test_tags.html')


@tags_bp.route('/api/tags/popular', methods=['GET'])
def get_popular_tags():
    """Get most used tags globally"""
    limit = request.args.get('limit', 20, type=int)
    
    popular_tags = Tag.query.filter(Tag.usage_count > 0).order_by(desc(Tag.usage_count)).limit(limit).all()
    
    return jsonify({
        'tags': [tag.to_dict() for tag in popular_tags]
    }), 200


@tags_bp.route('/api/tags/search', methods=['GET'])
def search_tags():
    """Search/autocomplete tags"""
    query = request.args.get('q', '').strip().lower()
    limit = request.args.get('limit', 10, type=int)
    
    if not query:
        return jsonify({'tags': []}), 200
    
    # Search tags that start with or contain the query
    matching_tags = Tag.query.filter(
        Tag.name.like(f'%{query}%')
    ).order_by(desc(Tag.usage_count)).limit(limit).all()
    
    return jsonify({
        'tags': [tag.to_dict() for tag in matching_tags]
    }), 200


@tags_bp.route('/api/users/<int:user_id>/tags', methods=['GET'])
def get_user_tags(user_id):
    """Get a user's tags (all tags they've used)"""
    # Get distinct tags used by this user
    user_tags = db.session.query(Tag).join(UserMediaTag).filter(
        UserMediaTag.user_id == user_id
    ).order_by(Tag.name).all()
    
    # Count how many times each tag is used by this user
    tag_counts = db.session.query(
        Tag.id,
        func.count(UserMediaTag.id).label('count')
    ).join(UserMediaTag).filter(
        UserMediaTag.user_id == user_id
    ).group_by(Tag.id).all()
    
    tag_count_map = {tag_id: count for tag_id, count in tag_counts}
    
    return jsonify({
        'tags': [
            {
                **tag.to_dict(),
                'user_usage_count': tag_count_map.get(tag.id, 0)
            }
            for tag in user_tags
        ]
    }), 200


@tags_bp.route('/api/media/<int:media_id>/tags', methods=['GET'])
def get_media_tags(media_id):
    """Get tags for a specific media item (for current user)"""
    media_type = request.args.get('media_type', 'movie')
    user_id = request.args.get('user_id', type=int)
    
    if not user_id:
        return jsonify({'error': 'user_id is required'}), 400
    
    # Get tags applied by this user to this media
    user_media_tags = UserMediaTag.query.join(Tag).filter(
        UserMediaTag.user_id == user_id,
        UserMediaTag.media_id == media_id,
        UserMediaTag.media_type == media_type
    ).all()
    
    return jsonify({
        'tags': [umt.tag.to_dict() for umt in user_media_tags]
    }), 200


@tags_bp.route('/api/media/<int:media_id>/tags', methods=['POST'])
@login_required
def add_tags_to_media(media_id):
    """Add tags to a media item"""
    # Check authentication for API routes
    if not current_user.is_authenticated:
        return jsonify({'error': 'Authentication required'}), 401
    
    data = request.get_json()
    
    if not data or 'tags' not in data or 'media_type' not in data:
        return jsonify({'error': 'tags and media_type are required'}), 400
    
    tag_names = data['tags']
    media_type = data['media_type']
    
    if not isinstance(tag_names, list):
        return jsonify({'error': 'tags must be an array'}), 400
    
    if not tag_names:
        return jsonify({'error': 'At least one tag is required'}), 400
    
    if len(tag_names) > 20:
        return jsonify({'error': 'Maximum 20 tags allowed per media'}), 400
    
    # Normalize and validate tag names
    normalized_tags = []
    for tag_name in tag_names:
        tag_name = tag_name.strip().lower()
        if not tag_name:
            continue
        if len(tag_name) > 30:
            return jsonify({'error': f'Tag "{tag_name}" exceeds 30 characters'}), 400
        normalized_tags.append(tag_name)
    
    if not normalized_tags:
        return jsonify({'error': 'No valid tags provided'}), 400
    
    added_tags = []
    
    for tag_name in normalized_tags:
        # Get or create tag
        tag = Tag.query.filter_by(name=tag_name).first()
        if not tag:
            tag = Tag(name=tag_name, usage_count=0)
            db.session.add(tag)
            db.session.flush()  # Get the tag ID
        
        # Check if user already tagged this media with this tag
        existing = UserMediaTag.query.filter_by(
            user_id=current_user.id,
            media_id=media_id,
            media_type=media_type,
            tag_id=tag.id
        ).first()
        
        if not existing:
            # Create new user media tag
            user_media_tag = UserMediaTag(
                user_id=current_user.id,
                media_id=media_id,
                media_type=media_type,
                tag_id=tag.id
            )
            db.session.add(user_media_tag)
            
            # Increment tag usage count
            tag.usage_count += 1
            
            added_tags.append(tag)
    
    db.session.commit()
    
    return jsonify({
        'message': 'Tags added successfully',
        'tags': [tag.to_dict() for tag in added_tags]
    }), 201


@tags_bp.route('/api/media/<int:media_id>/tags/<int:tag_id>', methods=['DELETE'])
@login_required
def remove_tag_from_media(media_id, tag_id):
    """Remove a tag from a media item"""
    # Check authentication for API routes
    if not current_user.is_authenticated:
        return jsonify({'error': 'Authentication required'}), 401
    
    media_type = request.args.get('media_type', 'movie')
    
    # Find the user media tag
    user_media_tag = UserMediaTag.query.filter_by(
        user_id=current_user.id,
        media_id=media_id,
        media_type=media_type,
        tag_id=tag_id
    ).first()
    
    if not user_media_tag:
        return jsonify({'error': 'Tag not found'}), 404
    
    # Get the tag to update usage count
    tag = Tag.query.get(tag_id)
    if tag:
        tag.usage_count = max(0, tag.usage_count - 1)
    
    db.session.delete(user_media_tag)
    db.session.commit()
    
    return jsonify({'message': 'Tag removed successfully'}), 200


@tags_bp.route('/api/tags/<tag_name>/media', methods=['GET'])
@login_required
def get_media_by_tag(tag_name):
    """Get all media tagged with a specific tag by the current user"""
    # Check authentication for API routes
    if not current_user.is_authenticated:
        return jsonify({'error': 'Authentication required'}), 401
    
    tag_name = tag_name.strip().lower()
    media_type = request.args.get('media_type')  # Optional filter by type
    
    # Find the tag
    tag = Tag.query.filter_by(name=tag_name).first()
    if not tag:
        return jsonify({'media': []}), 200
    
    # Get all media tagged by current user with this tag
    query = UserMediaTag.query.filter_by(
        user_id=current_user.id,
        tag_id=tag.id
    )
    
    if media_type:
        query = query.filter_by(media_type=media_type)
    
    user_media_tags = query.order_by(desc(UserMediaTag.created_at)).all()
    
    # Build response with media info (we'll need to fetch from TMDB or cache)
    media_list = []
    for umt in user_media_tags:
        media_list.append({
            'id': umt.media_id,
            'media_type': umt.media_type,
            'tagged_at': umt.created_at.isoformat()
        })
    
    return jsonify({
        'tag': tag.to_dict(),
        'media': media_list
    }), 200
