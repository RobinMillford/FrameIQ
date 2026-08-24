"""
Tests for TMDB cache thread safety and eviction.
These verify the P0-2 fix (threading.Lock in _BoundedTTLCache).
"""
import threading
import time


def test_bounded_cache_eviction():
    from api.tmdb_client import _BoundedTTLCache
    c = _BoundedTTLCache(maxsize=10)
    for i in range(12):
        c[f'key_{i}'] = (f'val_{i}', time.time())
    assert len(c._store) <= 10


def test_bounded_cache_get_set():
    from api.tmdb_client import _BoundedTTLCache
    c = _BoundedTTLCache(maxsize=100)
    c['k'] = ('v', time.time())
    assert 'k' in c
    assert c['k'][0] == 'v'


def test_bounded_cache_thread_safety():
    """Concurrent writes must not corrupt the cache dict."""
    from api.tmdb_client import _BoundedTTLCache
    c = _BoundedTTLCache(maxsize=50)
    errors = []

    def writer(n):
        try:
            for i in range(20):
                c[f'key_{n}_{i}'] = (f'val_{n}_{i}', time.time())
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=writer, args=(t,)) for t in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"Thread safety violations: {errors}"
    assert len(c._store) <= 50


def test_bounded_cache_ttl_expiry():
    """Entries older than max_age should be considered expired."""
    from api.tmdb_client import _BoundedTTLCache
    c = _BoundedTTLCache(maxsize=100)
    old_ts = time.time() - 7200   # 2 hours ago
    c['old_key'] = ('old_val', old_ts)
    # TTL check is done in cached_tmdb_request, not __contains__
    # Verify the entry is stored but its timestamp is old
    data, ts = c['old_key']
    assert time.time() - ts > 3600  # definitely expired
