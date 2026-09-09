"""
Watch Routes — video streaming pages + watch-intent tracking.

Continue Watching is intent-based ("FrameIQ remembers WHAT I started; the
provider remembers WHERE I stopped"): opening a watch page records a START;
playback position belongs entirely to the third-party provider. FrameIQ does
not track currentTime/duration/progress, listen for player events, or poll
iframes. Completion is explicit (the user presses "✓ Finished").
"""
import logging

from flask import Blueprint, render_template, request, jsonify
from flask_login import login_required, current_user

from models import db, MediaItem, user_watchlist
from extensions import limiter
from api.stream_providers import (
    PROVIDERS, DEFAULT_PROVIDER, get_sources,
)
from utils.request_guard import expensive_page_limit
from api import continue_watching as cw

watch_bp = Blueprint('watch', __name__)
logger = logging.getLogger(__name__)


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

    # Watch-intent START (idempotent). Cheap upsert; must never break the
    # watch page. Playback position is the provider's business, not ours.
    if current_user.is_authenticated:
        try:
            cw.start_item(
                current_user.id, 'movie', tmdb_id,
                title=movie.get('title'),
                poster_path=movie.get('poster_path'),
            )
        except Exception:
            db.session.rollback()
            logger.warning("Continue Watching start failed for movie %s",
                           tmdb_id, exc_info=True)

    provider = request.args.get('provider', DEFAULT_PROVIDER)
    if provider not in PROVIDERS:
        provider = DEFAULT_PROVIDER
    sources = get_sources('movie', tmdb_id)
    embed_url = next(
        (s['url'] for s in sources if s['key'] == provider), sources[0]['url']
    )
    return render_template('watch_movie.html',
                           movie=movie, tmdb_id=tmdb_id,
                           sources=sources, active_provider=provider,
                           embed_url=embed_url,
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

    # Watch-intent START for this exact episode (idempotent). The Continue
    # action must reopen exactly this season/episode; the provider resumes
    # playback position on its own.
    if current_user.is_authenticated:
        try:
            cw.start_item(
                current_user.id, media_type if media_type == 'anime' else 'tv',
                tmdb_id, season=season, episode=episode,
                title=show.get('name'),
                poster_path=show.get('poster_path'),
            )
        except Exception:
            db.session.rollback()
            logger.warning("Continue Watching start failed for show %s S%sE%s",
                           tmdb_id, season, episode, exc_info=True)

    provider = request.args.get('provider', DEFAULT_PROVIDER)
    if provider not in PROVIDERS:
        provider = DEFAULT_PROVIDER
    # Always build TV-style embeds here, even when type=anime
    sources = get_sources(
        'tv', tmdb_id, season=season, episode=episode,
    )
    embed_url = next(
        (s['url'] for s in sources if s['key'] == provider), sources[0]['url']
    )
    return render_template('watch_tv.html',
                           show=show, tmdb_id=tmdb_id,
                           season=season, episode=episode,
                           media_type=media_type,
                           sources=sources, active_provider=provider,
                           embed_url=embed_url,
                           in_watchlist=_is_in_watchlist(tmdb_id, media_type))


# ── Continue Watching API (intent-based) ─────────────────────────────────────
# START happens server-side on watch-page open. These endpoints cover the
# explicit actions: finish (records canonical watched state) and remove
# (hides the item WITHOUT marking it watched).

@watch_bp.route('/api/continue-watching')
@login_required
def continue_watching_list():
    """Canonical Continue Watching entries for the current user."""
    entries = cw.continue_watching_entries(current_user.id)
    return jsonify({'items': entries}), 200


@watch_bp.route('/api/continue-watching/movie/<int:tmdb_id>/start',
                methods=['POST'])
@login_required
@limiter.limit("60 per minute")
def cw_start_movie(tmdb_id):
    """Record that the user started this movie (idempotent)."""
    try:
        data = request.get_json(silent=True) or {}
        cw.start_item(
            current_user.id, 'movie', tmdb_id,
            title=data.get('title'),
            poster_path=data.get('poster_path'),
        )
        return jsonify({'success': True, 'started': True}), 200
    except Exception:
        db.session.rollback()
        logger.error("CW start movie failed", exc_info=True)
        return jsonify({'error': 'Could not record start'}), 500


@watch_bp.route('/api/continue-watching/movie/<int:tmdb_id>/finish',
                methods=['POST'])
@login_required
@limiter.limit("60 per minute")
def cw_finish_movie(tmdb_id):
    """Mark the movie watched via canonical watched-state and remove it from
    Continue Watching. Deterministic — no playback inference."""
    try:
        data = request.get_json(silent=True) or {}
        title = data.get('title')
        poster_path = data.get('poster_path')
        if not title:
            details = cw.movie_details(tmdb_id)
            if details:
                title = details.get('title')
                poster_path = poster_path or details.get('poster_path')
        result = cw.mark_movie_finished(
            current_user.id, tmdb_id, title=title, poster_path=poster_path)
        if not result.get('success'):
            return jsonify({'error': result.get('error', 'Could not finish')}), 400
        return jsonify({'success': True, 'watched': True,
                        'is_rewatch': result.get('is_rewatch', False),
                        'title': result.get('title')}), 200
    except Exception:
        db.session.rollback()
        logger.error("CW finish movie failed", exc_info=True)
        return jsonify({'error': 'Could not mark finished'}), 500


@watch_bp.route('/api/continue-watching/movie/<int:tmdb_id>/remove',
                methods=['POST'])
@login_required
@limiter.limit("60 per minute")
def cw_remove_movie(tmdb_id):
    """Remove the movie from Continue Watching WITHOUT marking it watched."""
    try:
        removed = cw.remove_item(current_user.id, 'movie', tmdb_id)
        return jsonify({'success': True, 'removed': removed}), 200
    except Exception:
        db.session.rollback()
        logger.error("CW remove movie failed", exc_info=True)
        return jsonify({'error': 'Could not remove item'}), 500


@watch_bp.route(
    '/api/continue-watching/tv/<int:show_id>/<int:season>/<int:episode>/start',
    methods=['POST'])
@login_required
@limiter.limit("60 per minute")
def cw_start_episode(show_id, season, episode):
    """Record that the user started this exact episode (idempotent)."""
    try:
        data = request.get_json(silent=True) or {}
        cw.start_item(
            current_user.id, 'tv', show_id, season=season, episode=episode,
            title=data.get('title'),
            poster_path=data.get('poster_path'),
        )
        return jsonify({'success': True, 'started': True}), 200
    except Exception:
        db.session.rollback()
        logger.error("CW start episode failed", exc_info=True)
        return jsonify({'error': 'Could not record start'}), 500


@watch_bp.route(
    '/api/continue-watching/tv/<int:show_id>/<int:season>/<int:episode>/finish',
    methods=['POST'])
@login_required
@limiter.limit("60 per minute")
def cw_finish_episode(show_id, season, episode):
    """Mark the exact episode watched (canonical TV tracking), remove it from
    Continue Watching, and promote the next valid unwatched episode."""
    try:
        result = cw.finish_tv_episode(
            current_user.id, show_id, season, episode)
        return jsonify({'success': True, **result}), 200
    except Exception:
        db.session.rollback()
        logger.error("CW finish episode failed", exc_info=True)
        return jsonify({'error': 'Could not mark finished'}), 500


@watch_bp.route(
    '/api/continue-watching/tv/<int:show_id>/<int:season>/<int:episode>/remove',
    methods=['POST'])
@login_required
@limiter.limit("60 per minute")
def cw_remove_episode(show_id, season, episode):
    """Remove the episode item from Continue Watching WITHOUT marking it
    watched."""
    try:
        removed = cw.remove_item(
            current_user.id, 'tv', show_id, season=season, episode=episode)
        return jsonify({'success': True, 'removed': removed}), 200
    except Exception:
        db.session.rollback()
        logger.error("CW remove episode failed", exc_info=True)
        return jsonify({'error': 'Could not remove item'}), 500


# ── Watch history (read-only; WatchProgress rows are legacy resume records) ──

@watch_bp.route('/api/watch/history')
@login_required
def watch_history():
    page = request.args.get('page', 1, type=int)
    per_page = min(request.args.get('per_page', 20, type=int), 100)
    from models import WatchProgress
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
