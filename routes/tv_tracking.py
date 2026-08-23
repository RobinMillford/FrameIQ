"""TV show tracking API: episode/season progress, status, bulk operations."""
import logging
from datetime import datetime

from flask import jsonify, request
from flask_login import current_user, login_required

from api.tmdb_client import cached_tmdb_request, fetch_tv_show_details
from api.tmdb.config import TMDB_API_KEY
from models import TVEpisodeWatch, TVShowProgress, db
from routes._tv_bp import TMDB_BASE_URL, tv_tracking

# Register page + calendar routes on the shared blueprint so app.py's single
# register_blueprint() call picks up every endpoint.
from routes import tv_calendar as _calendar_routes  # noqa: F401,E402
from routes import tv_pages as _page_routes  # noqa: F401,E402

logger = logging.getLogger(__name__)


@tv_tracking.route('/api/tv/<int:show_id>/start-tracking', methods=['POST'])
@login_required
def start_tracking_show(show_id):
    """Start tracking a TV show"""
    try:
        # Check if already tracking
        existing = TVShowProgress.query.filter_by(
            user_id=current_user.id,
            show_id=show_id
        ).first()
        
        if existing:
            return jsonify({'error': 'Already tracking this show'}), 400
        
        # Fetch show details from TMDb
        show = fetch_tv_show_details(show_id)
        
        # Create progress entry
        progress = TVShowProgress(
            user_id=current_user.id,
            show_id=show_id,
            total_seasons=show.get('number_of_seasons', 0),
            total_episodes=show.get('number_of_episodes', 0),
            watched_seasons=0,
            watched_episodes=0,
            status='watching'
        )
        
        db.session.add(progress)
        db.session.commit()
        
        return jsonify({
            'success': True,
            'message': 'Started tracking show',
            'progress': progress.to_dict()
        }), 201
        
    except Exception as e:
        db.session.rollback()
        logger.error("Unexpected error in tv_tracking", exc_info=True)
        return jsonify({'error': 'An unexpected error occurred'}), 500


@tv_tracking.route('/api/tv/<int:show_id>/progress', methods=['GET'])
@login_required
def get_show_progress(show_id):
    """Get user's progress for a TV show"""
    try:
        progress = TVShowProgress.query.filter_by(
            user_id=current_user.id,
            show_id=show_id
        ).first()
        
        if not progress:
            return jsonify({'progress': None}), 200
        
        # Refresh total episodes count from TMDb to catch new releases
        try:
            show = fetch_tv_show_details(show_id)
            current_total = show.get('number_of_episodes', 0)
            
            if current_total != progress.total_episodes:
                logger.debug("Show %s episode count changed: %s → %s", show_id, progress.total_episodes, current_total)
                progress.total_episodes = current_total
                progress.total_seasons = show.get('number_of_seasons', 0)

                if current_total > progress.watched_episodes and progress.status == 'completed':
                    show_status = show.get('status', '')
                    if show_status not in ['Ended', 'Canceled']:
                        progress.status = 'watching'
                        logger.debug("Show %s: reverted status to 'watching' - new episodes available", show_id)

                db.session.commit()
        except Exception as e:
            logger.warning("Could not refresh show %s data: %s", show_id, e)
        
        # Get watched episodes
        watched_episodes = TVEpisodeWatch.query.filter_by(
            user_id=current_user.id,
            show_id=show_id
        ).order_by(
            TVEpisodeWatch.season_number,
            TVEpisodeWatch.episode_number
        ).all()
        
        return jsonify({
            'progress': progress.to_dict(),
            'watched_episodes': [ep.to_dict() for ep in watched_episodes]
        }), 200
        
    except Exception as e:
        logger.error("Unexpected error in tv_tracking", exc_info=True)
        return jsonify({'error': 'An unexpected error occurred'}), 500


@tv_tracking.route('/api/tv/<int:show_id>/episode/<int:season>/<int:episode>/mark-watched', methods=['POST'])
@login_required
def mark_episode_watched(show_id, season, episode):
    """Mark an episode as watched"""
    try:
        logger.debug("Mark episode watched: show=%s S%sE%s user=%s", show_id, season, episode, current_user.id)

        data = request.get_json(silent=True) or {}

        # Get or create progress entry
        progress = TVShowProgress.query.filter_by(
            user_id=current_user.id,
            show_id=show_id
        ).first()

        if not progress:
            show = fetch_tv_show_details(show_id)
            progress = TVShowProgress(
                user_id=current_user.id,
                show_id=show_id,
                total_seasons=show.get('number_of_seasons', 0),
                total_episodes=show.get('number_of_episodes', 0),
                watched_seasons=0,
                watched_episodes=0,
                status='watching'
            )
            db.session.add(progress)
            db.session.flush()
            logger.debug("Created progress id=%s for show %s", progress.id, show_id)
        else:
            logger.debug("Using progress id=%s for show %s", progress.id, show_id)
        
        # Check if episode already marked
        existing = TVEpisodeWatch.query.filter_by(
            user_id=current_user.id,
            show_id=show_id,
            season_number=season,
            episode_number=episode
        ).first()
        
        if existing:
            existing.watched_date = datetime.strptime(data.get('watched_date', datetime.utcnow().strftime('%Y-%m-%d')), '%Y-%m-%d').date()
            existing.rating = data.get('rating')
            existing.notes = data.get('notes')
            existing.is_rewatch = data.get('is_rewatch', False)
            episode_watch = existing
        else:
            episode_watch = TVEpisodeWatch(
                user_id=current_user.id,
                show_id=show_id,
                progress_id=progress.id,
                season_number=season,
                episode_number=episode,
                episode_name=data.get('episode_name'),
                watched_date=datetime.strptime(data.get('watched_date', datetime.utcnow().strftime('%Y-%m-%d')), '%Y-%m-%d').date(),
                rating=data.get('rating'),
                notes=data.get('notes'),
                is_rewatch=data.get('is_rewatch', False)
            )
            db.session.add(episode_watch)

            if not episode_watch.is_rewatch:
                progress.watched_episodes += 1
                logger.debug("Progress: %s/%s", progress.watched_episodes, progress.total_episodes)
        
        # Update last watched time
        progress.last_watched = datetime.utcnow()
        
        # Check if season completed
        update_season_progress(progress, show_id)
        
        # Check if show completed - but only mark as completed if show has actually ended
        # For returning series, keep status as 'watching' even if all current episodes are watched
        if progress.watched_episodes >= progress.total_episodes and progress.total_episodes > 0:
            # Fetch show details to check if it's actually ended
            try:
                show = fetch_tv_show_details(show_id)
                show_status = show.get('status', '')
                
                # Only mark as completed if show has actually ended
                if show_status in ['Ended', 'Canceled']:
                    progress.status = 'completed'
                    progress.completed_at = datetime.utcnow()
                    logger.debug("Show %s marked COMPLETED (status: %s)", show_id, show_status)
                else:
                    logger.debug("Show %s: all episodes watched but status='%s', keeping 'watching'", show_id, show_status)
                    if progress.status == 'completed':
                        progress.status = 'watching'
            except Exception as e:
                logger.warning("Could not fetch show %s status: %s", show_id, e)

        db.session.commit()

        return jsonify({
            'success': True,
            'message': 'Episode marked as watched',
            'progress': progress.to_dict()
        }), 200

    except Exception as e:
        db.session.rollback()
        logger.error("Unexpected error in tv_tracking", exc_info=True)
        return jsonify({'error': 'An unexpected error occurred'}), 500


@tv_tracking.route('/api/tv/<int:show_id>/season/<int:season>/mark-watched', methods=['POST'])
@login_required
def mark_season_watched(show_id, season):
    """Mark entire season as watched"""
    try:
        logger.debug("Mark season watched: show=%s season=%s user=%s", show_id, season, current_user.id)

        data = request.get_json() or {}
        watched_date = data.get('watched_date', datetime.utcnow().strftime('%Y-%m-%d'))

        season_url = f'{TMDB_BASE_URL}/tv/{show_id}/season/{season}?api_key={TMDB_API_KEY}'
        season_data = cached_tmdb_request(season_url)
        if not season_data:
            return jsonify({'error': 'Failed to fetch season details'}), 400
        episodes = season_data.get('episodes', [])
        logger.debug("Season %s of show %s has %s episodes", season, show_id, len(episodes))

        progress = TVShowProgress.query.filter_by(
            user_id=current_user.id,
            show_id=show_id
        ).first()

        if not progress:
            show = fetch_tv_show_details(show_id)
            progress = TVShowProgress(
                user_id=current_user.id,
                show_id=show_id,
                total_seasons=show.get('number_of_seasons', 0),
                total_episodes=show.get('number_of_episodes', 0),
                watched_seasons=0,
                watched_episodes=0,
                status='watching'
            )
            db.session.add(progress)
            db.session.flush()
            logger.debug("Created progress id=%s for show %s", progress.id, show_id)
        else:
            logger.debug("Progress id=%s: %s/%s watched", progress.id, progress.watched_episodes, progress.total_episodes)
        
        # IMPORTANT: Delete existing episodes for this season first to ensure clean state
        existing_count = TVEpisodeWatch.query.filter_by(
            user_id=current_user.id,
            show_id=show_id,
            season_number=season
        ).count()
        
        if existing_count > 0:
            logger.debug("Deleting %s existing episodes for show %s season %s", existing_count, show_id, season)
            TVEpisodeWatch.query.filter_by(
                user_id=current_user.id,
                show_id=show_id,
                season_number=season
            ).delete()
            db.session.flush()
            progress.watched_episodes -= existing_count
        
        # Mark all episodes in season
        marked_count = 0
        for ep in episodes:
            ep_num = ep['episode_number']
            
            episode_watch = TVEpisodeWatch(
                user_id=current_user.id,
                show_id=show_id,
                progress_id=progress.id,
                season_number=season,
                episode_number=ep_num,
                episode_name=ep.get('name'),
                watched_date=datetime.strptime(watched_date, '%Y-%m-%d').date()
            )
            db.session.add(episode_watch)
            marked_count += 1
        
        # Update progress
        old_watched = progress.watched_episodes
        progress.watched_episodes += marked_count
        progress.last_watched = datetime.utcnow()
        update_season_progress(progress, show_id)
        logger.debug("Progress: %s → %s / %s", old_watched, progress.watched_episodes, progress.total_episodes)
        
        # Check completion - but only mark as completed if show has actually ended
        if progress.watched_episodes >= progress.total_episodes and progress.total_episodes > 0:
            # Fetch show details to check if it's actually ended
            try:
                show = fetch_tv_show_details(show_id)
                show_status = show.get('status', '')
                
                # Only mark as completed if show has actually ended
                if show_status in ['Ended', 'Canceled']:
                    progress.status = 'completed'
                    progress.completed_at = datetime.utcnow()
                    logger.debug("Show %s marked COMPLETED (status: %s)", show_id, show_status)
                else:
                    logger.debug("Show %s: all episodes watched but status='%s', keeping 'watching'", show_id, show_status)
                    if progress.status == 'completed':
                        progress.status = 'watching'
            except Exception as e:
                logger.warning("Could not fetch show %s status: %s", show_id, e)

        db.session.commit()

        return jsonify({
            'success': True,
            'message': f'Season {season} marked as watched',
            'marked_episodes': marked_count,
            'progress': progress.to_dict()
        }), 200

    except Exception as e:
        db.session.rollback()
        logger.error("Unexpected error in tv_tracking", exc_info=True)
        return jsonify({'error': 'An unexpected error occurred'}), 500


@tv_tracking.route('/api/tv/my-shows', methods=['GET'])
@login_required
def get_my_tracked_shows():
    """Get all shows user is tracking"""
    try:
        status_filter = request.args.get('status')  # 'watching', 'completed', 'plan_to_watch', 'dropped'
        
        query = TVShowProgress.query.filter_by(user_id=current_user.id)
        
        if status_filter:
            query = query.filter_by(status=status_filter)
        
        shows = query.order_by(TVShowProgress.last_watched.desc()).all()
        
        return jsonify({
            'shows': [show.to_dict() for show in shows],
            'total': len(shows)
        }), 200
        
    except Exception as e:
        logger.error("Unexpected error in tv_tracking", exc_info=True)
        return jsonify({'error': 'An unexpected error occurred'}), 500


@tv_tracking.route('/api/tv/<int:show_id>/update-status', methods=['POST'])
@login_required
def update_show_status(show_id):
    """Update show tracking status"""
    try:
        data = request.get_json()
        new_status = data.get('status')
        
        if new_status not in ['watching', 'completed', 'plan_to_watch', 'dropped']:
            return jsonify({'error': 'Invalid status'}), 400
        
        progress = TVShowProgress.query.filter_by(
            user_id=current_user.id,
            show_id=show_id
        ).first()
        
        if not progress:
            return jsonify({'error': 'Show not being tracked'}), 404
        
        progress.status = new_status
        
        if new_status == 'completed':
            progress.completed_at = datetime.utcnow()
        elif progress.completed_at:
            progress.completed_at = None
        
        db.session.commit()
        
        return jsonify({
            'success': True,
            'progress': progress.to_dict()
        }), 200
        
    except Exception as e:
        db.session.rollback()
        logger.error("Unexpected error in tv_tracking", exc_info=True)
        return jsonify({'error': 'An unexpected error occurred'}), 500


@tv_tracking.route('/api/tv/<int:show_id>/episode/<int:season_number>/<int:episode_number>/update-watch', methods=['POST'])
@login_required
def update_episode_watch(show_id, season_number, episode_number):
    """Update episode watch with rating and notes"""
    try:
        data = request.get_json()
        
        # Find or create episode watch
        watch = TVEpisodeWatch.query.filter_by(
            user_id=current_user.id,
            show_id=show_id,
            season_number=season_number,
            episode_number=episode_number
        ).first()
        
        if not watch:
            watch = TVEpisodeWatch(
                user_id=current_user.id,
                show_id=show_id,
                season_number=season_number,
                episode_number=episode_number,
                watched_at=datetime.utcnow()
            )
            db.session.add(watch)
        
        # Update fields
        watch.rating = data.get('rating')
        watch.notes = data.get('notes')
        watch.is_rewatch = data.get('is_rewatch', False)
        
        db.session.commit()
        
        return jsonify({'success': True})
    except Exception as e:
        db.session.rollback()
        logger.error("Unexpected error in tv_tracking", exc_info=True)
        return jsonify({'success': False, 'error': 'An unexpected error occurred'}), 500


@tv_tracking.route('/api/tv/<int:show_id>/watched-episodes')
@login_required
def get_watched_episodes(show_id):
    """Get all watched episodes for a show"""
    try:
        episodes = TVEpisodeWatch.query.filter_by(
            user_id=current_user.id,
            show_id=show_id
        ).all()

        return jsonify({
            'success': True,
            'episodes': [{
                'season_number': ep.season_number,
                'episode_number': ep.episode_number,
                'watched_date': ep.watched_date.isoformat() if ep.watched_date else None,
                'rating': ep.rating,
                'notes': ep.notes,
                'is_rewatch': ep.is_rewatch
            } for ep in episodes]
        })
    except Exception as e:
        logger.error("Error in get_watched_episodes: %s", e, exc_info=True)
        return jsonify({'success': False, 'error': 'An unexpected error occurred'}), 500


@tv_tracking.route('/api/tv/<int:show_id>/season/<int:season_number>/unmark-watched', methods=['POST'])
@login_required
def unmark_season_watched(show_id, season_number):
    """Unmark all episodes in a season as unwatched"""
    try:
        TVEpisodeWatch.query.filter_by(
            user_id=current_user.id,
            show_id=show_id,
            season_number=season_number
        ).delete()

        progress = TVShowProgress.query.filter_by(
            user_id=current_user.id, show_id=show_id
        ).first()
        if progress:
            progress.watched_episodes = TVEpisodeWatch.query.filter_by(
                user_id=current_user.id, show_id=show_id
            ).count()
            update_season_progress(progress, show_id)

        db.session.commit()
        return jsonify({'success': True})
    except Exception as e:
        db.session.rollback()
        logger.error("Unexpected error in tv_tracking", exc_info=True)
        return jsonify({'success': False, 'error': 'An unexpected error occurred'}), 500


@tv_tracking.route('/api/tv/<int:show_id>/episode/<int:season_number>/<int:episode_number>/unmark-watched', methods=['POST'])
@login_required
def unmark_single_episode(show_id, season_number, episode_number):
    """Unmark a single episode as unwatched (new version)"""
    try:
        TVEpisodeWatch.query.filter_by(
            user_id=current_user.id,
            show_id=show_id,
            season_number=season_number,
            episode_number=episode_number
        ).delete()

        progress = TVShowProgress.query.filter_by(
            user_id=current_user.id, show_id=show_id
        ).first()
        if progress:
            progress.watched_episodes = max(0, TVEpisodeWatch.query.filter_by(
                user_id=current_user.id, show_id=show_id
            ).count())
            update_season_progress(progress, show_id)

        db.session.commit()
        return jsonify({'success': True})
    except Exception as e:
        db.session.rollback()
        logger.error("Unexpected error in tv_tracking", exc_info=True)
        return jsonify({'success': False, 'error': 'An unexpected error occurred'}), 500


@tv_tracking.route('/api/tv/<int:show_id>/mark-all-watched', methods=['POST'])
@login_required
def mark_all_watched(show_id):
    """Mark all episodes in all seasons as watched (complete series)"""
    try:
        # Fetch show details (cached)
        show_data = fetch_tv_show_details(show_id)
        if not show_data:
            return jsonify({'success': False, 'error': 'Failed to fetch show details'}), 400
        show_status = show_data.get('status', '')

        # Mark each episode in each season
        for season in show_data.get('seasons', []):
            if season['season_number'] == 0:  # Skip specials
                continue

            # Fetch season details (cached)
            season_url = f'{TMDB_BASE_URL}/tv/{show_id}/season/{season["season_number"]}?api_key={TMDB_API_KEY}'
            season_data = cached_tmdb_request(season_url)
            if not season_data:
                continue
            
            # Mark each episode
            for episode in season_data.get('episodes', []):
                # Check if already watched
                existing = TVEpisodeWatch.query.filter_by(
                    user_id=current_user.id,
                    show_id=show_id,
                    season_number=season['season_number'],
                    episode_number=episode['episode_number']
                ).first()
                
                if not existing:
                    watch = TVEpisodeWatch(
                        user_id=current_user.id,
                        show_id=show_id,
                        season_number=season['season_number'],
                        episode_number=episode['episode_number'],
                        watched_date=datetime.utcnow().date()
                    )
                    db.session.add(watch)
        
        # Update show progress to completed
        progress = TVShowProgress.query.filter_by(
            user_id=current_user.id,
            show_id=show_id
        ).first()
        
        if progress:
            # Sync watched_episodes from actual DB count (HI-01)
            db.session.flush()
            progress.watched_episodes = TVEpisodeWatch.query.filter_by(
                user_id=current_user.id, show_id=show_id
            ).count()
            if show_status in ['Ended', 'Canceled']:
                progress.status = 'completed'
                progress.completed_at = datetime.utcnow()
            else:
                progress.status = 'watching'

        db.session.commit()

        return jsonify({'success': True, 'message': 'Series completed!'})
    except Exception as e:
        db.session.rollback()
        logger.error("Unexpected error in tv_tracking", exc_info=True)
        return jsonify({'success': False, 'error': 'An unexpected error occurred'}), 500


def update_season_progress(progress, show_id):
    """Update watched seasons count based on completed seasons"""
    try:
        show_data = fetch_tv_show_details(show_id)
        if not show_data:
            return
        seasons = show_data.get('seasons', [])
        
        # Count completed seasons
        completed_seasons = 0
        for season in seasons:
            if season['season_number'] == 0:  # Skip specials
                continue
            
            season_num = season['season_number']
            episode_count = season['episode_count']
            
            # Count watched episodes in this season
            watched_in_season = TVEpisodeWatch.query.filter_by(
                user_id=progress.user_id,
                show_id=show_id,
                season_number=season_num
            ).filter(TVEpisodeWatch.is_rewatch == False).count()
            
            if watched_in_season >= episode_count:
                completed_seasons += 1
        
        progress.watched_seasons = completed_seasons
        
    except Exception as e:
        logger.warning("Error updating season progress: %s", e)
