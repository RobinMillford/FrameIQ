"""Proxy TMDB API calls server-side — keeps the API key off the browser."""
import re
import os
import requests
from flask import Blueprint, request, jsonify
from extensions import limiter

tmdb_proxy_bp = Blueprint('tmdb_proxy', __name__)

_TMDB_API_KEY = os.getenv('TMDB_API_KEY')
_TMDB_BASE_URL = 'https://api.themoviedb.org/3'
_SAFE_PATH = re.compile(r'^/[a-zA-Z0-9/_-]+$')


@tmdb_proxy_bp.route('/api/tmdb/proxy')
@limiter.limit("180 per minute")
def tmdb_proxy():
    path = request.args.get('path', '')
    if not _SAFE_PATH.match(path):
        return jsonify({'error': 'Invalid TMDB path'}), 400

    params = {k: v for k, v in request.args.items() if k not in ('path', 'api_key')}
    params['api_key'] = _TMDB_API_KEY

    try:
        resp = requests.get(f"{_TMDB_BASE_URL}{path}", params=params, timeout=8)
        return jsonify(resp.json()), resp.status_code
    except requests.exceptions.RequestException:
        return jsonify({'error': 'TMDB request failed'}), 502
