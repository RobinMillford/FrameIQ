"""Proxy TMDB API calls server-side — keeps the API key off the browser."""
import hashlib
import re
import os
import threading
import time
from copy import deepcopy

import requests
from flask import Blueprint, request, jsonify
from extensions import limiter

tmdb_proxy_bp = Blueprint('tmdb_proxy', __name__)

_TMDB_API_KEY = os.getenv('TMDB_API_KEY')
_TMDB_BASE_URL = 'https://api.themoviedb.org/3'
_SAFE_PATH = re.compile(r'^/[a-zA-Z0-9/_-]+$')

# Only endpoint families the app's frontend actually uses. Anything else
# is rejected before any upstream request is made.
_ALLOWED_PREFIXES = (
    '/trending/',
    '/movie/',
    '/tv/',
    '/search/',
    '/discover/',
)
_PROXY_TIMEOUT = (3, 10)
_PROXY_MAX_RETRIES = 1
_RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}

# Small bounded TTL cache with a dedicated key namespace so attacker-chosen
# proxy params can never evict the main application tmdb_cache entries.
_PROXY_CACHE_MAX = 300
_PROXY_CACHE_TTL = 600  # seconds — proxy payloads are user-facing lists
_proxy_cache = {}
_proxy_cache_lock = threading.Lock()


def _proxy_cache_get(cache_key):
    with _proxy_cache_lock:
        entry = _proxy_cache.get(cache_key)
        if entry is None:
            return None
        data, ts = entry
        if time.time() - ts >= _PROXY_CACHE_TTL:
            del _proxy_cache[cache_key]
            return None
        return deepcopy(data)


def _proxy_cache_set(cache_key, data):
    with _proxy_cache_lock:
        if len(_proxy_cache) >= _PROXY_CACHE_MAX:
            # Evict oldest 10% by timestamp.
            oldest = sorted(_proxy_cache.items(), key=lambda kv: kv[1][1])
            for k, _ in oldest[:max(1, _PROXY_CACHE_MAX // 10)]:
                del _proxy_cache[k]
        _proxy_cache[cache_key] = (deepcopy(data), time.time())


def _fetch_upstream(url, params):
    """GET the TMDB upstream with bounded retries for transient failures.

    Returns (parsed_json_dict, upstream_status_code) or (None, None) on
    network failure.
    """
    last_exc = None
    for attempt in range(_PROXY_MAX_RETRIES + 1):
        try:
            resp = requests.get(url, params=params, timeout=_PROXY_TIMEOUT)
            if resp.status_code in _RETRYABLE_STATUS_CODES and attempt < _PROXY_MAX_RETRIES:
                time.sleep(0.25 * (attempt + 1))
                continue
            try:
                return resp.json(), resp.status_code
            except ValueError:
                # Non-JSON upstream body (error page, HTML, truncation).
                return None, None
        except requests.exceptions.RequestException as exc:
            last_exc = exc
            break
    if last_exc is not None:
        return None, None
    return None, None


@tmdb_proxy_bp.route('/api/tmdb/proxy')
@limiter.limit("180 per minute")
def tmdb_proxy():
    path = request.args.get('path', '')
    if not path or not _SAFE_PATH.match(path):
        return jsonify({'error': 'Invalid TMDB path'}), 400

    if not path.startswith(_ALLOWED_PREFIXES):
        return jsonify({'error': 'Unsupported TMDB path'}), 400

    # Cache key covers the full query (path + params) but never the api_key
    # value itself, and is namespaced away from the application cache.
    cache_params = {k: v for k, v in request.args.items() if k != 'api_key'}
    cache_key = 'proxy:' + hashlib.md5(
        (path + '?' + str(sorted(cache_params.items()))).encode()
    ).hexdigest()

    cached = _proxy_cache_get(cache_key)
    if cached is not None:
        return jsonify(cached), 200

    params = dict(cache_params)
    params['api_key'] = _TMDB_API_KEY

    data, status = _fetch_upstream(f"{_TMDB_BASE_URL}{path}", params)
    if data is None:
        return jsonify({'error': 'TMDB request failed'}), 502

    if status == 200:
        _proxy_cache_set(cache_key, data)
    # Preserve upstream status handling for non-200 responses (404, etc.).

    return jsonify(data), status
