"""Shared 'main' blueprint object.

The physical route modules (browse.py, collections.py, main.py) all attach
endpoints to this single blueprint so that every endpoint name
('main.index', 'main.watchlist', ...) stays stable for url_for() calls.
"""
from flask import Blueprint

main = Blueprint('main', __name__)
