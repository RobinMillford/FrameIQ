"""
Watch Routes — video streaming pages + progress tracking
"""
import os
import logging
from datetime import datetime, date

from flask import Blueprint, render_template, request, jsonify
from flask_login import login_required, current_user

from models import db, WatchProgress, MediaItem, DiaryEntry

watch_bp = Blueprint('watch', __name__)
logger = logging.getLogger(__name__)

TMDB_API_KEY = os.getenv('TMDB_API_KEY')
EMBED_BASE = 'https://www.vidking.net'


# ── Page routes ──────────────────────────────────────────────────────────────

@watch_bp.route('/watch/movie/<int:tmdb_id>')
def watch_movie(tmdb_id):
    from api.tmdb_client import fetch_movie_details
    try:
        movie = fetch_movie_details(tmdb_id)
    except Exception as e:
        logger.warning("Could not fetch movie %s: %s", tmdb_id, e)
        movie = {'id': tmdb_id, 'title': 'Unknown', 'poster_path': None,
                 'overview': '', 'release_date': '', 'genres': [],
                 'vote_average': 0, 'recommendations': []}

    resume_time = 0
    if current_user.is_authenticated:
        wp = WatchProgress.query.filter_by(
            user_id=current_user.id, tmdb_id=tmdb_id, media_type='movie',
            season=None, episode=None
        ).first()
        if wp and wp.progress_pct < 90:
            resume_time = int(wp.current_time)

    embed_url = f'{EMBED_BASE}/embed/movie/{tmdb_id}'
    return render_template('watch_movie.html',
                           movie=movie, tmdb_id=tmdb_id,
                           embed_url=embed_url, resume_time=resume_time)


@watch_bp.route('/watch/tv/<int:tmdb_id>/<int:season>/<int:episode>')
def watch_tv(tmdb_id, season, episode):
    from api.tmdb_client import fetch_tv_show_details
    media_type = request.args.get('type', 'tv')

    try:
        show = fetch_tv_show_details(tmdb_id)
    except Exception as e:
        logger.warning("Could not fetch show %s: %s", tmdb_id, e)
        show = {'id': tmdb_id, 'name': 'Unknown Show', 'seasons': [],
                'poster_path': None, 'overview': '', 'status': '',
                'number_of_seasons': 0, 'vote_average': 0}

    resume_time = 0
    if current_user.is_authenticated:
        wp = WatchProgress.query.filter_by(
            user_id=current_user.id, tmdb_id=tmdb_id, media_type=media_type,
            season=season, episode=episode
        ).first()
        if wp and wp.progress_pct < 90:
            resume_time = int(wp.current_time)

    embed_url = f'{EMBED_BASE}/embed/tv/{tmdb_id}/{season}/{episode}'
    return render_template('watch_tv.html',
                           show=show, tmdb_id=tmdb_id,
                           season=season, episode=episode,
                           media_type=media_type,
                           embed_url=embed_url, resume_time=resume_time)


# ── API routes ────────────────────────────────────────────────────────────────

@watch_bp.route('/api/watch/progress', methods=['POST'])
@login_required
def save_progress():
    data = request.get_json()
    if not data:
        return jsonify({'error': 'JSON required'}), 400

    tmdb_id = data.get('id')
    if not tmdb_id:
        return jsonify({'error': 'id required'}), 400

    media_type = data.get('mediaType', 'movie')
    current_time = float(data.get('currentTime', 0))
    duration = float(data.get('duration', 0))
    progress = float(data.get('progress', 0))
    season = data.get('season')
    episode = data.get('episode')
    title = data.get('title', '')
    poster_path = data.get('posterPath', '')

    try:
        wp = WatchProgress.query.filter_by(
            user_id=current_user.id,
            tmdb_id=tmdb_id,
            media_type=media_type,
            season=season,
            episode=episode
        ).first()

        if wp:
            wp.current_time = current_time
            wp.duration = duration
            wp.updated_at = datetime.utcnow()
            if title:
                wp.title = title
            if poster_path:
                wp.poster_path = poster_path
        else:
            wp = WatchProgress(
                user_id=current_user.id,
                tmdb_id=tmdb_id,
                media_type=media_type,
                season=season,
                episode=episode,
                current_time=current_time,
                duration=duration,
                title=title,
                poster_path=poster_path,
            )
            db.session.add(wp)

        auto_logged = False
        if progress >= 85 and duration > 60:
            auto_logged = _auto_log(tmdb_id, media_type, title, poster_path)

        db.session.commit()
        return jsonify({'success': True, 'auto_logged': auto_logged}), 200

    except Exception as e:
        db.session.rollback()
        logger.error("Progress save error: %s", e, exc_info=True)
        return jsonify({'error': 'Could not save progress'}), 500


@watch_bp.route('/api/watch/continue')
@login_required
def continue_watching():
    items = (
        WatchProgress.query
        .filter(
            WatchProgress.user_id == current_user.id,
            WatchProgress.duration > 60,
        )
        .order_by(WatchProgress.updated_at.desc())
        .limit(20)
        .all()
    )
    # Filter out fully-watched in Python (progress_pct uses property)
    in_progress = [i for i in items if i.progress_pct < 90]
    return jsonify({'items': [i.to_dict() for i in in_progress]}), 200


@watch_bp.route('/api/watch/history')
@login_required
def watch_history():
    page = request.args.get('page', 1, type=int)
    per_page = min(request.args.get('per_page', 20, type=int), 100)
    pagination = (
        WatchProgress.query
        .filter_by(user_id=current_user.id)
        .order_by(WatchProgress.updated_at.desc())
        .paginate(page=page, per_page=per_page, error_out=False)
    )
    return jsonify({
        'items': [i.to_dict() for i in pagination.items],
        'total': pagination.total,
        'pages': pagination.pages,
        'current_page': page,
    }), 200


# ── Internal helpers ──────────────────────────────────────────────────────────

def _auto_log(tmdb_id, media_type, title, poster_path):
    """Log to diary when >= 85 % watched. No-op if already logged today."""
    try:
        media = MediaItem.query.filter_by(
            tmdb_id=tmdb_id, media_type=media_type
        ).first()
        if not media:
            media = MediaItem(
                tmdb_id=tmdb_id, media_type=media_type,
                title=title, poster_path=poster_path
            )
            db.session.add(media)
            db.session.flush()

        today = date.today()
        already = DiaryEntry.query.filter_by(
            user_id=current_user.id,
            media_id=media.id,
            watched_date=today
        ).first()
        if not already:
            db.session.add(DiaryEntry(
                user_id=current_user.id,
                media_id=media.id,
                media_type=media_type,
                watched_date=today,
                is_rewatch=False,
            ))
            return True
    except Exception as e:
        logger.warning("Auto-log diary failed: %s", e)
    return False
