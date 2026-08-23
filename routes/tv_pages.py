"""TV tracking page routes: dashboard, calendar, my-shows, season/episode pages."""
import logging

import requests
from flask import render_template
from flask_login import login_required

from api.tmdb.config import TMDB_API_KEY
from routes._tv_bp import TMDB_BASE_URL, tv_tracking

logger = logging.getLogger(__name__)


@tv_tracking.route('/tv/dashboard')
@login_required
def tv_dashboard():
    """TV Tracking Dashboard - main page for TV tracking"""
    return render_template('tv_dashboard.html')


@tv_tracking.route('/tv/upcoming')
@login_required
def tv_upcoming():
    """Upcoming episodes page with filters"""
    return render_template('tv_upcoming.html')


@tv_tracking.route('/tv/calendar')
@login_required
def tv_calendar_page():
    """Render TV calendar page"""
    return render_template('tv_calendar.html')


@tv_tracking.route('/tv/my-shows')
@login_required
def my_shows_page():
    """Render my shows tracking page"""
    return render_template('tv_my_shows.html')


@tv_tracking.route('/tv/<int:show_id>/season/<int:season_number>')
@login_required
def season_detail(show_id, season_number):
    """Season detail page with episode list"""
    try:
        response = requests.get(
            f'{TMDB_BASE_URL}/tv/{show_id}',
            params={'api_key': TMDB_API_KEY},
            timeout=8
        )
        response.raise_for_status()
        show_name = response.json().get('name', 'Unknown Show')
    except Exception as e:
        logger.warning("Could not fetch show %s name: %s", show_id, e)
        show_name = 'Unknown Show'

    return render_template(
        'tv_season_detail.html',
        show_id=show_id,
        show_name=show_name,
        season_number=season_number,
    )


@tv_tracking.route('/tv/<int:show_id>/season/<int:season_number>/episode/<int:episode_number>')
@login_required
def episode_detail(show_id, season_number, episode_number):
    """Episode detail page with watch controls"""
    try:
        response = requests.get(
            f'{TMDB_BASE_URL}/tv/{show_id}',
            params={'api_key': TMDB_API_KEY},
            timeout=8
        )
        response.raise_for_status()
        show_name = response.json().get('name', 'Unknown Show')
    except Exception as e:
        logger.warning("Could not fetch show %s name: %s", show_id, e)
        show_name = 'Unknown Show'

    return render_template(
        'tv_episode_detail.html',
        show_id=show_id,
        show_name=show_name,
        season_number=season_number,
        episode_number=episode_number,
    )
