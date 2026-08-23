"""Thread-safe bounded TTL cache for TMDb responses."""
import hashlib
import logging
import threading
import time

import requests

logger = logging.getLogger(__name__)


class _BoundedTTLCache:
    """Thread-safe size-bounded TTL cache; evicts oldest 10% when full."""

    def __init__(self, maxsize=500):
        self._store = {}
        self._maxsize = maxsize
        self._lock = threading.Lock()

    def __contains__(self, key):
        with self._lock:
            return key in self._store

    def __getitem__(self, key):
        with self._lock:
            return self._store[key]

    def __setitem__(self, key, value):
        with self._lock:
            if len(self._store) >= self._maxsize:
                oldest = sorted(self._store.items(), key=lambda x: x[1][1])
                for k, _ in oldest[:max(1, self._maxsize // 10)]:
                    del self._store[k]
            self._store[key] = value


# Bounded in-memory cache for TMDB data (max 500 entries, TTL checked on read)
tmdb_cache = _BoundedTTLCache(maxsize=500)


def get_cache_key(*args):
    """Generate a cache key from arguments"""
    return hashlib.md5(str(args).encode()).hexdigest()


def cached_tmdb_request(url, max_age=3600):
    """Make a TMDB request with caching"""
    cache_key = get_cache_key(url)
    current_time = time.time()

    # Check if we have a cached response that's still valid
    if cache_key in tmdb_cache:
        cached_data, timestamp = tmdb_cache[cache_key]
        if current_time - timestamp < max_age:
            logger.debug("TMDB cache hit: %s", url.split('?')[0])
            return cached_data

    logger.debug("TMDB request: %s", url.split('?')[0])
    response = requests.get(url)
    data = response.json()

    # Cache the response
    tmdb_cache[cache_key] = (data, current_time)
    return data
