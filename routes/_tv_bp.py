"""Shared blueprint object for TV tracking routes.

Physical modules (tv_pages.py, tv_calendar.py, tv_tracking.py) attach
endpoints to this one blueprint so every endpoint name
('tv_tracking.*') stays stable for url_for() calls.
"""
from flask import Blueprint

tv_tracking = Blueprint('tv_tracking', __name__)

TMDB_BASE_URL = 'https://api.themoviedb.org/3'
