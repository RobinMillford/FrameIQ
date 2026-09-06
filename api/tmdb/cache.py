"""Thread-safe bounded TTL cache for TMDb responses."""
import hashlib
import logging
import random
import threading
import time
from copy import deepcopy

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
_RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}
_MAX_RETRIES = 1
_DEFAULT_TIMEOUT = (3, 5)


class _InFlightRequest:
    """Coordinate concurrent requests for the same cache key."""

    def __init__(self):
        self.event = threading.Event()
        self.result = None
        self.error = None


_inflight_requests = {}
_inflight_lock = threading.Lock()


def get_cache_key(*args):
    """Generate a cache key from arguments"""
    return hashlib.md5(str(args).encode()).hexdigest()


def cached_tmdb_request(
    url, max_age=3600, timeout=_DEFAULT_TIMEOUT, max_retries=_MAX_RETRIES,
    deadline=None,
):
    """Make a cached TMDb request with bounded, status-aware retries."""
    max_retries = min(max(0, int(max_retries)), _MAX_RETRIES)
    cache_key = get_cache_key(url)
    current_time = time.time()

    # Check if we have a cached response that's still valid
    if cache_key in tmdb_cache:
        cached_data, timestamp = tmdb_cache[cache_key]
        if current_time - timestamp < max_age:
            logger.debug("TMDB cache hit: %s", url.split('?')[0])
            return deepcopy(cached_data)

    endpoint = url.split('?')[0]
    with _inflight_lock:
        request = _inflight_requests.get(cache_key)
        is_leader = request is None
        if is_leader:
            request = _InFlightRequest()
            _inflight_requests[cache_key] = request

    if not is_leader:
        wait_timeout = None
        if deadline is not None:
            wait_timeout = max(0, deadline - time.monotonic())
        if not request.event.wait(wait_timeout):
            raise requests.exceptions.Timeout("TMDB request deadline exceeded")
        if request.error is not None:
            raise request.error
        return deepcopy(request.result)

    try:
        for attempt in range(max_retries + 1):
            try:
                request_timeout = timeout
                if deadline is not None:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise requests.exceptions.Timeout(
                            "TMDB request deadline exceeded"
                        )
                    request_timeout = (
                        min(timeout[0], remaining),
                        min(timeout[1], remaining),
                    )
                logger.debug(
                    "TMDB request endpoint=%s attempt=%s/%s",
                    endpoint, attempt + 1, max_retries + 1,
                )
                response = requests.get(url, timeout=request_timeout)
                if response.status_code == 404:
                    logger.info("TMDB not found endpoint=%s status=404", endpoint)
                    data = {
                        "success": False,
                        "status_code": 404,
                        "status_message": "Not found",
                    }
                    tmdb_cache[cache_key] = (data, time.time())
                    request.result = data
                    return deepcopy(data)
                response.raise_for_status()
                data = response.json()
                tmdb_cache[cache_key] = (data, time.time())
                request.result = data
                return deepcopy(data)
            except requests.exceptions.RequestException as exc:
                status = getattr(exc.response, "status_code", None)
                retryable = (
                    status in _RETRYABLE_STATUS_CODES
                    or status is None
                )
                if not retryable or attempt >= max_retries:
                    logger.warning(
                        "TMDB request failed endpoint=%s status=%s retries=%s reason=%s",
                        endpoint, status, attempt, exc,
                    )
                    if not retryable:
                        request.result = {
                            "success": False,
                            "status_code": status,
                            "status_message": str(exc),
                        }
                        return deepcopy(request.result)
                    request.error = exc
                    raise
                delay = (
                    min(1.0, 0.25 * (2 ** attempt))
                    + random.uniform(0, 0.15)
                )
                if deadline is not None:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        request.error = requests.exceptions.Timeout(
                            "TMDB request deadline exceeded"
                        )
                        raise request.error
                    delay = min(delay, remaining)
                logger.warning(
                    "TMDB request retrying endpoint=%s status=%s retry=%s/%s",
                    endpoint, status, attempt + 1, max_retries,
                )
                time.sleep(delay)
    except Exception as exc:
        request.error = exc
        raise
    finally:
        with _inflight_lock:
            _inflight_requests.pop(cache_key, None)
        request.event.set()
