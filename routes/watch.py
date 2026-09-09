"""
Watch Routes — video streaming pages + progress tracking
"""
import os
import logging
from datetime import datetime, date

from flask import Blueprint, render_template, request, jsonify
from flask_login import login_required, current_user
from sqlalchemy.exc import IntegrityError

from models import db, WatchProgress, MediaItem, DiaryEntry, user_watchlist
from extensions import limiter
from api.stream_providers import (
    PROVIDERS, DEFAULT_PROVIDER, ALLOWED_ORIGINS, get_sources,
)
from utils.request_guard import expensive_page_limit

watch_bp = Blueprint('watch', __name__)
logger = logging.getLogger(__name__)

TMDB_API_KEY = os.getenv('TMDB_API_KEY')


# ── Page routes ──────────────────────────────────────────────────────────────

def _is_in_watchlist(tmdb_id, media_type):
    """Whether the current user already has this TMDb item watchlisted."""
    if not current_user.is_authenticated:
        return False
    media = MediaItem.query.filter_by(
        tmdb_id=tmdb_id, media_type=media_type
    ).first()
    if not media:
        return False
    stmt = db.select(user_watchlist.c.user_id).where(
        user_watchlist.c.user_id == current_user.id,
        user_watchlist.c.media_id == media.id,
        user_watchlist.c.media_type == media_type,
    )
    return db.session.execute(stmt).fetchone() is not None


@watch_bp.route('/watch/movie/<int:tmdb_id>')
@expensive_page_limit
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

    provider = request.args.get('provider', DEFAULT_PROVIDER)
    if provider not in PROVIDERS:
        provider = DEFAULT_PROVIDER
    sources = get_sources('movie', tmdb_id, resume_time=resume_time)
    embed_url = next(
        (s['url'] for s in sources if s['key'] == provider), sources[0]['url']
    )
    return render_template('watch_movie.html',
                           movie=movie, tmdb_id=tmdb_id,
                           sources=sources, active_provider=provider,
                           allowed_origins=ALLOWED_ORIGINS,
                           embed_url=embed_url, resume_time=resume_time,
                           in_watchlist=_is_in_watchlist(tmdb_id, 'movie'))


@watch_bp.route('/watch/tv/<int:tmdb_id>/<int:season>/<int:episode>')
@expensive_page_limit
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

    provider = request.args.get('provider', DEFAULT_PROVIDER)
    if provider not in PROVIDERS:
        provider = DEFAULT_PROVIDER
    # Always build TV-style embeds here, even when type=anime
    sources = get_sources(
        'tv', tmdb_id, season=season, episode=episode,
        resume_time=resume_time,
    )
    embed_url = next(
        (s['url'] for s in sources if s['key'] == provider), sources[0]['url']
    )
    return render_template('watch_tv.html',
                           show=show, tmdb_id=tmdb_id,
                           season=season, episode=episode,
                           media_type=media_type,
                           sources=sources, active_provider=provider,
                           allowed_origins=ALLOWED_ORIGINS,
                           embed_url=embed_url, resume_time=resume_time,
                           in_watchlist=_is_in_watchlist(tmdb_id, media_type))


# ── API routes ────────────────────────────────────────────────────────────────

@watch_bp.route('/api/watch/progress', methods=['POST'])
@login_required
@limiter.limit("30 per minute")
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

    # Some providers report position+duration without a progress percentage;
    # derive it so completion detection and progress_pct stay consistent.
    if duration > 0 and progress <= 0 and current_time > 0:
        progress = min(100.0, current_time / duration * 100.0)

    try:
        _upsert_progress_row(
            current_user.id, tmdb_id, media_type, season, episode,
            current_time, duration, title, poster_path)

        auto_logged = False
        completed = progress >= 85 and duration > 60
        if completed:
            try:
                with db.session.begin_nested():
                    auto_logged = _auto_log(tmdb_id, media_type, title, poster_path)
            except Exception as ae:
                logger.warning("Auto-log savepoint failed: %s", ae)

        db.session.commit()
        return jsonify({'success': True, 'auto_logged': auto_logged,
                        'completed': completed}), 200

    except IntegrityError:
        db.session.rollback()
        # Race condition: another request inserted same row; retry as update
        _handle_progress_race(
            current_user.id, tmdb_id, media_type, season, episode,
            current_time, duration)
        return jsonify({'success': True, 'auto_logged': False}), 200
    except Exception as e:
        db.session.rollback()
        logger.error("Progress save error: %s", e, exc_info=True)
        return jsonify({'error': 'Could not save progress'}), 500


@watch_bp.route('/api/watch/continue')
@login_required
def continue_watching():
    """Canonical resumable items for the player-page resume rail.

    Uses the same validated aggregation as the home rail (api.continue_watching):
    canonical metadata, TV episodes validated against TMDb season data, and a
    completed-state cross-check (diary for movies, TVEpisodeWatch for TV) so
    stale resume rows never reappear after real completion.
    """
    from api.continue_watching import continue_watching_entries
    items = continue_watching_entries(current_user.id, movie_limit=10, tv_limit=10)
    return jsonify({'items': items}), 200


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

def _upsert_progress_row(user_id, tmdb_id, media_type, season, episode,
                         current_time, duration, title, poster_path):
    """Create or update the user's resume row. Zero-valued telemetry is
    ignored on update: providers that emit partial messages (e.g. progress
    without duration) must never overwrite a real saved position."""
    wp = WatchProgress.query.filter_by(
        user_id=user_id, tmdb_id=tmdb_id, media_type=media_type,
        season=season, episode=episode
    ).first()
    if wp:
        if duration > 0:
            wp.duration = duration
        if current_time > 0:
            wp.current_time = current_time
        wp.updated_at = datetime.utcnow()
        if title:
            wp.title = title
        if poster_path:
            wp.poster_path = poster_path
    else:
        wp = WatchProgress(
            user_id=user_id,
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
    return wp


def _handle_progress_race(user_id, tmdb_id, media_type, season, episode,
                          current_time, duration):
    """IntegrityError recovery: re-fetch the row another request inserted
    and update it in place (same zero-telemetry guard)."""
    wp = WatchProgress.query.filter_by(
        user_id=user_id, tmdb_id=tmdb_id, media_type=media_type,
        season=season, episode=episode
    ).first()
    if wp:
        if duration > 0:
            wp.duration = duration
        if current_time > 0:
            wp.current_time = current_time
        wp.updated_at = datetime.utcnow()
        db.session.commit()


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
