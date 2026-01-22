"""
Lists API Routes
Handles user-created custom lists of movies/TV shows
"""
from flask import Blueprint, request, jsonify, render_template
from flask_login import login_required, current_user
from models import db, User, UserList, UserListItem, MediaItem
from sqlalchemy.exc import IntegrityError
from datetime import datetime
import requests
import os

lists = Blueprint('lists', __name__)

TMDB_API_KEY = os.getenv("TMDB_API_KEY")


@lists.route('/api/lists', methods=['GET'])
def get_public_lists():
    """Get all public lists"""
    page = request.args.get('page', 1, type=int)
    per_page = request.args.get('per_page', 20, type=int)
    
    # Query public lists
    lists_query = UserList.query.filter_by(is_public=True).order_by(UserList.created_at.desc())
    
    # Paginate
    pagination = lists_query.paginate(page=page, per_page=per_page, error_out=False)
    
    return jsonify({
        'lists': [user_list.to_dict() for user_list in pagination.items],
        'total': pagination.total,
        'pages': pagination.pages,
        'current_page': page,
        'has_next': pagination.has_next,
        'has_prev': pagination.has_prev
    }), 200


@lists.route('/api/users/<int:user_id>/lists', methods=['GET'])
def get_user_lists(user_id):
    """Get lists for a specific user"""
    user = User.query.get_or_404(user_id)
    
    # If viewing own lists, show all. Otherwise, only public
    if current_user.is_authenticated and current_user.id == user_id:
        user_lists = user.lists.order_by(UserList.created_at.desc()).all()
    else:
        user_lists = user.lists.filter_by(is_public=True).order_by(UserList.created_at.desc()).all()
    
    return jsonify({
        'lists': [user_list.to_dict() for user_list in user_lists],
        'count': len(user_lists)
    }), 200


@lists.route('/api/lists/<int:list_id>', methods=['GET'])
def get_list_details(list_id):
    """Get details of a specific list including all items"""
    user_list = UserList.query.get_or_404(list_id)
    
    # Check permissions
    if not user_list.is_public and (not current_user.is_authenticated or current_user.id != user_list.user_id):
        return jsonify({'error': 'This list is private'}), 403
    
    # Get all items in the list
    items = user_list.items.order_by(UserListItem.position, UserListItem.added_at).all()
    
    list_data = user_list.to_dict()
    list_data['items'] = [item.to_dict() for item in items]
    
    return jsonify(list_data), 200


@lists.route('/api/lists/create', methods=['POST'])
@login_required
def create_list():
    """Create a new list"""
    data = request.get_json()
    
    if not data or not data.get('title'):
        return jsonify({'error': 'List title is required'}), 400
    
    try:
        new_list = UserList(
            user_id=current_user.id,
            title=data['title'],
            description=data.get('description', ''),
            is_public=data.get('is_public', True)
        )
        
        db.session.add(new_list)
        db.session.commit()
        
        return jsonify({
            'message': 'List created successfully',
            'list': new_list.to_dict()
        }), 201
        
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 500


@lists.route('/api/lists/<int:list_id>/update', methods=['PUT'])
@login_required
def update_list(list_id):
    """Update list details"""
    user_list = UserList.query.get_or_404(list_id)
    
    # Check ownership
    if user_list.user_id != current_user.id:
        return jsonify({'error': 'You can only edit your own lists'}), 403
    
    data = request.get_json()
    
    try:
        if 'title' in data:
            user_list.title = data['title']
        if 'description' in data:
            user_list.description = data['description']
        if 'is_public' in data:
            user_list.is_public = data['is_public']
        
        user_list.updated_at = datetime.utcnow()
        db.session.commit()
        
        return jsonify({
            'message': 'List updated successfully',
            'list': user_list.to_dict()
        }), 200
        
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 500


@lists.route('/api/lists/<int:list_id>/delete', methods=['DELETE'])
@login_required
def delete_list(list_id):
    """Delete a list"""
    user_list = UserList.query.get_or_404(list_id)
    
    # Check ownership
    if user_list.user_id != current_user.id:
        return jsonify({'error': 'You can only delete your own lists'}), 403
    
    try:
        db.session.delete(user_list)
        db.session.commit()
        
        return jsonify({'message': 'List deleted successfully'}), 200
        
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 500


@lists.route('/api/lists/<int:list_id>/add', methods=['POST'])
@login_required
def add_to_list(list_id):
    """Add a media item to a list"""
    user_list = UserList.query.get_or_404(list_id)
    
    # Check ownership
    if user_list.user_id != current_user.id:
        return jsonify({'error': 'You can only add items to your own lists'}), 403
    
    data = request.get_json()
    media_id = data.get('media_id')
    media_type = data.get('media_type')
    
    if not media_id or not media_type:
        return jsonify({'error': 'media_id and media_type are required'}), 400
    
    try:
        # Check if media item exists in our database, if not create it
        media_item = MediaItem.query.filter_by(tmdb_id=media_id, media_type=media_type).first()
        if not media_item:
            # Fetch from TMDB API
            url = f"https://api.themoviedb.org/3/{media_type}/{media_id}?api_key={TMDB_API_KEY}"
            response = requests.get(url)
            if response.status_code == 200:
                tmdb_data = response.json()
                title = tmdb_data.get('title') if media_type == 'movie' else tmdb_data.get('name')
                release_date_str = tmdb_data.get('release_date') if media_type == 'movie' else tmdb_data.get('first_air_date')
                
                # Convert string date to Python date object
                release_date = None
                if release_date_str:
                    try:
                        release_date = datetime.strptime(release_date_str, '%Y-%m-%d').date()
                    except ValueError:
                        release_date = None
                
                media_item = MediaItem(
                    tmdb_id=media_id,
                    media_type=media_type,
                    title=title,
                    release_date=release_date,
                    poster_path=tmdb_data.get('poster_path'),
                    overview=tmdb_data.get('overview'),
                    rating=tmdb_data.get('vote_average')
                )
                db.session.add(media_item)
                db.session.commit()
            else:
                return jsonify({'error': 'Media item not found'}), 404
        
        # Get the next position
        max_position = db.session.query(db.func.max(UserListItem.position)).filter_by(list_id=list_id).scalar()
        next_position = (max_position or 0) + 1
        
        # Add to list
        list_item = UserListItem(
            list_id=list_id,
            media_id=media_item.id,
            media_type=media_type,
            position=next_position,
            note=data.get('note', '')
        )
        
        db.session.add(list_item)
        user_list.updated_at = datetime.utcnow()
        db.session.commit()
        
        return jsonify({
            'message': f'Added {media_item.title} to list',
            'item': list_item.to_dict()
        }), 201
        
    except IntegrityError:
        db.session.rollback()
        return jsonify({'error': 'This item is already in the list'}), 400
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 500


@lists.route('/api/lists/<int:list_id>/remove/<int:item_id>', methods=['DELETE'])
@login_required
def remove_from_list(list_id, item_id):
    """Remove an item from a list"""
    user_list = UserList.query.get_or_404(list_id)
    
    # Check ownership
    if user_list.user_id != current_user.id:
        return jsonify({'error': 'You can only remove items from your own lists'}), 403
    
    list_item = UserListItem.query.get_or_404(item_id)
    
    # Verify item belongs to this list
    if list_item.list_id != list_id:
        return jsonify({'error': 'Item not found in this list'}), 404
    
    try:
        db.session.delete(list_item)
        user_list.updated_at = datetime.utcnow()
        db.session.commit()
        
        return jsonify({'message': 'Item removed from list'}), 200
        
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 500


# HTML Template Routes
@lists.route('/lists')
@login_required
def my_lists():
    """View all user's lists"""
    user_lists = current_user.lists.order_by(UserList.created_at.desc()).all()
    return render_template('my_lists.html', user_lists=user_lists)


@lists.route('/lists/<int:list_id>')
def view_list(list_id):
    """View a specific list with all its items"""
    user_list = UserList.query.get_or_404(list_id)
    
    # Check permissions
    if not user_list.is_public and (not current_user.is_authenticated or current_user.id != user_list.user_id):
        return render_template('error.html', message='This list is private'), 403
    
    # Get all items in the list with their media details
    items = user_list.items.order_by(UserListItem.position, UserListItem.added_at).all()
    
    return render_template('list_detail.html', user_list=user_list, items=items)
