"""TV episode calendar and upcoming-episode API routes."""
import logging
from datetime import datetime, timedelta

from flask import jsonify, request
from flask_login import current_user, login_required

from models import TVEpisodeWatch, TVShowProgress, UpcomingEpisode
from routes._tv_bp import tv_tracking

logger = logging.getLogger(__name__)


@tv_tracking.route('/api/tv/upcoming-episodes', methods=['GET'])
@login_required
def get_upcoming_episodes():
    """Get upcoming episodes for shows user is tracking"""
    try:
        # Get shows user is currently watching or planning to watch
        watching_shows = TVShowProgress.query.filter_by(
            user_id=current_user.id
        ).filter(TVShowProgress.status.in_(['watching', 'plan_to_watch'])).all()
        
        show_ids = [show.show_id for show in watching_shows]
        
        # Get upcoming episodes for these shows
        today = datetime.utcnow().date()
        week_from_now = today + timedelta(days=7)
        
        upcoming = UpcomingEpisode.query.filter(
            UpcomingEpisode.show_id.in_(show_ids),
            UpcomingEpisode.air_date >= today,
            UpcomingEpisode.air_date <= week_from_now
        ).order_by(UpcomingEpisode.air_date).all()
        
        episodes_list = []
        for ep in upcoming:
            # Check if already watched
            watched = TVEpisodeWatch.query.filter_by(
                user_id=current_user.id,
                show_id=ep.show_id,
                season_number=ep.season_number,
                episode_number=ep.episode_number
            ).first()
            
            if not watched:  # Only include unwatched episodes
                days_until = (ep.air_date - today).days
                ep_dict = ep.to_dict()
                ep_dict['days_until_air'] = days_until
                episodes_list.append(ep_dict)
        
        return jsonify({
            'success': True,
            'episodes': episodes_list
        }), 200
        
    except Exception as e:
        logger.error("Unexpected error in tv_tracking", exc_info=True)
        return jsonify({'error': 'An unexpected error occurred'}), 500


@tv_tracking.route('/api/tv/calendar', methods=['GET'])
@login_required
def get_episode_calendar():
    """Get calendar view of upcoming episodes"""
    try:
        # Get date range
        start_date = request.args.get('start_date')
        end_date = request.args.get('end_date')
        
        if start_date:
            start_date = datetime.strptime(start_date, '%Y-%m-%d').date()
        else:
            start_date = datetime.utcnow().date()
        
        if end_date:
            end_date = datetime.strptime(end_date, '%Y-%m-%d').date()
        else:
            end_date = start_date + timedelta(days=30)
        
        # Get shows user is tracking
        watching_shows = TVShowProgress.query.filter_by(
            user_id=current_user.id,
            status='watching'
        ).all()
        
        show_ids = [show.show_id for show in watching_shows]
        
        # Get upcoming episodes in date range
        episodes = UpcomingEpisode.query.filter(
            UpcomingEpisode.show_id.in_(show_ids),
            UpcomingEpisode.air_date >= start_date,
            UpcomingEpisode.air_date <= end_date
        ).order_by(UpcomingEpisode.air_date).all()
        
        # Group by date
        calendar = {}
        for episode in episodes:
            date_key = episode.air_date.isoformat()
            if date_key not in calendar:
                calendar[date_key] = []
            calendar[date_key].append(episode.to_dict())
        
        return jsonify({
            'calendar': calendar,
            'start_date': start_date.isoformat(),
            'end_date': end_date.isoformat()
        }), 200
        
    except Exception as e:
        logger.error("Unexpected error in tv_tracking", exc_info=True)
        return jsonify({'error': 'An unexpected error occurred'}), 500
