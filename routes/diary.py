"""
Diary API Routes
Handles user diary entries for logging watched movies/shows
"""
from flask import Blueprint, request, jsonify, render_template
from flask_login import login_required, current_user
from models import db, User, DiaryEntry, MediaItem, Review
from sqlalchemy.exc import IntegrityError
from datetime import datetime, date
import requests
import os

diary = Blueprint('diary', __name__)

TMDB_API_KEY = os.getenv("TMDB_API_KEY")


@diary.route('/diary')
@login_required
def diary_page():
    """Render the user's diary page"""
    return render_template('diary.html')


@diary.route('/api/diary', methods=['GET'])
@login_required
def get_diary_entries():
    """Get diary entries for the current user"""
    page = request.args.get('page', 1, type=int)
    per_page = request.args.get('per_page', 20, type=int)
    year = request.args.get('year', type=int)
    month = request.args.get('month', type=int)
    
    # Build query
    query = current_user.diary_entries.order_by(DiaryEntry.watched_date.desc(), DiaryEntry.created_at.desc())
    
    # Filter by year/month if provided
    if year:
        query = query.filter(db.extract('year', DiaryEntry.watched_date) == year)
    if month:
        query = query.filter(db.extract('month', DiaryEntry.watched_date) == month)
    
    # Paginate
    pagination = query.paginate(page=page, per_page=per_page, error_out=False)
    
    return jsonify({
        'entries': [entry.to_dict() for entry in pagination.items],
        'total': pagination.total,
        'pages': pagination.pages,
        'current_page': page,
        'has_next': pagination.has_next,
        'has_prev': pagination.has_prev
    }), 200


@diary.route('/api/users/<int:user_id>/diary', methods=['GET'])
def get_user_diary(user_id):
    """Get diary entries for a specific user (public view)"""
    user = User.query.get_or_404(user_id)
    
    page = request.args.get('page', 1, type=int)
    per_page = request.args.get('per_page', 20, type=int)
    
    # Query diary entries
    query = user.diary_entries.order_by(DiaryEntry.watched_date.desc(), DiaryEntry.created_at.desc())
    
    # Paginate
    pagination = query.paginate(page=page, per_page=per_page, error_out=False)
    
    return jsonify({
        'entries': [entry.to_dict() for entry in pagination.items],
        'total': pagination.total,
        'pages': pagination.pages,
        'current_page': page,
        'has_next': pagination.has_next,
        'has_prev': pagination.has_prev,
        'user': {
            'id': user.id,
            'username': user.username,
            'profile_picture': user.profile_picture
        }
    }), 200


@diary.route('/api/diary/log', methods=['POST'])
@login_required
def log_diary_entry():
    """Log a new diary entry"""
    data = request.get_json()
    
    media_id = data.get('media_id')
    media_type = data.get('media_type')
    watched_date_str = data.get('watched_date')
    
    if not media_id or not media_type or not watched_date_str:
        return jsonify({'error': 'media_id, media_type, and watched_date are required'}), 400
    
    try:
        # Parse watched date
        watched_date = datetime.strptime(watched_date_str, '%Y-%m-%d').date()
        
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
        
        # Check if this is a rewatch
        existing_entries = DiaryEntry.query.filter_by(
            user_id=current_user.id,
            media_id=media_item.id,
            media_type=media_type
        ).count()
        
        is_rewatch = existing_entries > 0
        
        # Create diary entry
        entry = DiaryEntry(
            user_id=current_user.id,
            media_id=media_item.id,
            media_type=media_type,
            watched_date=watched_date,
            rating=data.get('rating'),
            is_rewatch=is_rewatch
        )
        
        db.session.add(entry)
        
        # Update user's total movies watched count
        current_user.total_movies_watched = (current_user.total_movies_watched or 0) + 1
        
        db.session.commit()
        
        return jsonify({
            'message': f'Logged {media_item.title} to diary',
            'entry': entry.to_dict()
        }), 201
        
    except ValueError:
        return jsonify({'error': 'Invalid date format. Use YYYY-MM-DD'}), 400
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 500


@diary.route('/api/diary/<int:entry_id>/update', methods=['PUT'])
@login_required
def update_diary_entry(entry_id):
    """Update a diary entry"""
    entry = DiaryEntry.query.get_or_404(entry_id)
    
    # Check ownership
    if entry.user_id != current_user.id:
        return jsonify({'error': 'You can only edit your own diary entries'}), 403
    
    data = request.get_json()
    
    try:
        if 'watched_date' in data:
            entry.watched_date = datetime.strptime(data['watched_date'], '%Y-%m-%d').date()
        if 'rating' in data:
            entry.rating = data['rating']
        
        db.session.commit()
        
        return jsonify({
            'message': 'Diary entry updated successfully',
            'entry': entry.to_dict()
        }), 200
        
    except ValueError:
        return jsonify({'error': 'Invalid date format. Use YYYY-MM-DD'}), 400
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 500


@diary.route('/api/diary/<int:entry_id>/delete', methods=['DELETE'])
@login_required
def delete_diary_entry(entry_id):
    """Delete a diary entry"""
    entry = DiaryEntry.query.get_or_404(entry_id)
    
    # Check ownership
    if entry.user_id != current_user.id:
        return jsonify({'error': 'You can only delete your own diary entries'}), 403
    
    try:
        db.session.delete(entry)
        
        # Update user's total movies watched count
        current_user.total_movies_watched = max(0, (current_user.total_movies_watched or 0) - 1)
        
        db.session.commit()
        
        return jsonify({'message': 'Diary entry deleted successfully'}), 200
        
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 500
