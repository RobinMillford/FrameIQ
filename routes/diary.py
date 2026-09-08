"""
Diary API Routes
Handles user diary entries for logging watched movies/shows
"""
import logging
import threading
import time

from flask import Blueprint, request, jsonify, render_template
from flask_login import login_required, current_user
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import joinedload
from datetime import datetime, date
import requests
import os

from extensions import limiter
from models import db, User, DiaryEntry, MediaItem, user_viewed

logger = logging.getLogger(__name__)

diary = Blueprint('diary', __name__)

TMDB_API_KEY = os.getenv("TMDB_API_KEY")


# ── Quick log (Feature #1) ───────────────────────────────────────────────────
# Canonical watched state: DiaryEntry is the source of truth for movie watch
# history (one row per watch event, rewatches included). The user_viewed
# junction is a derived boolean set ("has watched at least once") kept in
# sync on every log event; migrates/migrate_canonical_watched.py reconciles
# pre-existing data once.

# Duplicate-submission window (double-click, rapid taps, browser retry).
_QUICKLOG_COOLDOWN_SECONDS = 3.0
_quicklog_guard = {}          # (user_id, tmdb_id, media_type) -> monotonic ts
_quicklog_guard_lock = threading.Lock()


def _quicklog_is_duplicate(user_id, tmdb_id, media_type):
    """True when the same quick-log action was submitted within the cooldown
    window; records this submission either way.

    Deliberately time-based rather than a DB unique constraint: two genuine
    watches on the same day are legitimate history and must both be
    recordable (the second is a rewatch). Only *rapid duplicate requests of
    the same action* are collapsed."""
    now = time.monotonic()
    key = (user_id, tmdb_id, media_type)
    with _quicklog_guard_lock:
        # Keep the guard dict bounded.
        if len(_quicklog_guard) > 10000:
            cutoff = now - _QUICKLOG_COOLDOWN_SECONDS * 20
            for stale in [k for k, ts in _quicklog_guard.items() if ts < cutoff]:
                _quicklog_guard.pop(stale, None)
        last = _quicklog_guard.get(key)
        _quicklog_guard[key] = now
    return last is not None and (now - last) < _QUICKLOG_COOLDOWN_SECONDS


def _get_or_create_movie(media_id, data):
    """Find the MediaItem for a TMDb movie id, creating it locally when
    possible.

    Prefers metadata sent from the card (title/poster/release date are
    already on the page), so a quick log normally performs no external
    request. Falls back to the shared TMDb-backed lookup only when we know
    nothing about the title."""
    media_item = MediaItem.query.filter_by(
        tmdb_id=media_id, media_type='movie').first()
    if media_item:
        return media_item

    title = (data.get('title') or '').strip()
    if title:
        release_date = None
        date_str = data.get('release_date') or ''
        if date_str:
            try:
                release_date = datetime.strptime(date_str[:10], '%Y-%m-%d').date()
            except ValueError:
                release_date = None
        media_item = MediaItem(
            tmdb_id=media_id,
            media_type='movie',
            title=title[:200],
            release_date=release_date,
            poster_path=(data.get('poster_path') or None),
        )
        db.session.add(media_item)
        db.session.flush()
        return media_item

    from utils.collections import get_or_create_media_item
    return get_or_create_media_item(media_id, 'movie')


@diary.route('/api/media/<int:media_id>/log', methods=['POST'])
@login_required
@limiter.limit("60 per minute")
def quick_log_movie(media_id):
    """One-tap movie logging.

    Creates today's watch event (a rewatch when the movie was already
    watched) and syncs the derived viewed state. Defaults: watched today,
    no rating, no review. Duplicate submissions inside the cooldown window
    return the first submission's outcome instead of duplicating history."""
    data = request.get_json(silent=True) or {}
    media_type = data.get('media_type', 'movie')
    if media_type != 'movie':
        return jsonify({'error': 'Quick log is for movies; TV tracking is separate'}), 400

    if _quicklog_is_duplicate(current_user.id, media_id, 'movie'):
        # Same action re-submitted: report the state the first submission
        # produced instead of creating a second event.
        return jsonify({
            'success': True,
            'watched': True,
            'logged_today': True,
            'duplicate': True,
        }), 200

    rating = data.get('rating')
    if rating is not None:
        try:
            rating = float(rating)
        except (TypeError, ValueError):
            return jsonify({'error': 'Invalid rating'}), 400
        if not (0.5 <= rating <= 5.0):
            return jsonify({'error': 'Rating must be between 0.5 and 5.0'}), 400

    try:
        media_item = _get_or_create_movie(media_id, data)
        if not media_item:
            return jsonify({'error': 'Media item not found'}), 404

        watch_count = DiaryEntry.query.filter_by(
            user_id=current_user.id,
            media_id=media_item.id,
            media_type='movie',
        ).count()
        is_rewatch = watch_count > 0

        entry = DiaryEntry(
            user_id=current_user.id,
            media_id=media_item.id,
            media_type='movie',
            watched_date=date.today(),
            rating=rating,
            is_rewatch=is_rewatch,
        )
        db.session.add(entry)

        # Mirror the existing /api/diary/log behavior.
        current_user.total_movies_watched = (
            current_user.total_movies_watched or 0) + 1

        # Sync the derived viewed set inside a savepoint so a concurrent
        # log's insert cannot discard our diary entry.
        try:
            with db.session.begin_nested():
                db.session.execute(user_viewed.insert().values(
                    user_id=current_user.id,
                    media_id=media_item.id,
                    media_type='movie',
                    date_viewed=datetime.utcnow(),
                ))
        except IntegrityError:
            pass  # viewed row already exists (concurrent log) — that's the goal

        db.session.commit()

        return jsonify({
            'success': True,
            'watched': True,
            'logged_today': True,
            'is_rewatch': is_rewatch,
            'watch_count': watch_count + 1,
            'entry_id': entry.id,
            'title': media_item.title,
        }), 201

    except Exception:
        db.session.rollback()
        logger.error("Quick log failed", exc_info=True)
        return jsonify({'error': 'Internal server error'}), 500


@diary.route('/diary')
@login_required
def diary_page():
    """Render the user's diary page"""
    return render_template('diary.html')


@diary.route('/api/diary', methods=['GET'])
@login_required
def get_diary_entries():  # noqa: F811 — defined below the quick-log helpers
    """Get diary entries for the current user"""
    page = request.args.get('page', 1, type=int)
    per_page = min(request.args.get('per_page', 20, type=int), 100)
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

    # Eager-load user/media so to_dict() doesn't lazy-load per entry (N+1)
    db.session.query(DiaryEntry).options(
        joinedload(DiaryEntry.user), joinedload(DiaryEntry.media)
    ).filter(DiaryEntry.id.in_([e.id for e in pagination.items])).all()

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
    per_page = min(request.args.get('per_page', 20, type=int), 100)

    # Query diary entries
    query = user.diary_entries.order_by(DiaryEntry.watched_date.desc(), DiaryEntry.created_at.desc())

    # Paginate
    pagination = query.paginate(page=page, per_page=per_page, error_out=False)

    # Eager-load user/media so to_dict() doesn't lazy-load per entry (N+1)
    db.session.query(DiaryEntry).options(
        joinedload(DiaryEntry.user), joinedload(DiaryEntry.media)
    ).filter(DiaryEntry.id.in_([e.id for e in pagination.items])).all()

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
            response = requests.get(url, timeout=(3, 10))
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
    except Exception:
        db.session.rollback()
        logger.error("Failed to add diary entry", exc_info=True)
        return jsonify({'error': 'Internal server error'}), 500


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
    except Exception:
        db.session.rollback()
        logger.error("Failed to update diary entry", exc_info=True)
        return jsonify({'error': 'Internal server error'}), 500


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

    except Exception:
        db.session.rollback()
        logger.error("Failed to delete diary entry", exc_info=True)
        return jsonify({'error': 'Internal server error'}), 500
