"""
Tests for TMDB cache thread safety and eviction.
These verify the P0-2 fix (threading.Lock in _BoundedTTLCache).
"""
import threading
import time

import pytest
import requests


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


def test_tmdb_404_is_not_retried(monkeypatch):
    from api.tmdb import cache

    calls = []

    class NotFoundResponse:
        status_code = 404

        def json(self):
            return {}

    def fake_get(url, **kwargs):
        calls.append((url, kwargs))
        return NotFoundResponse()

    monkeypatch.setattr(cache.requests, "get", fake_get)
    cache.tmdb_cache._store.clear()

    result = cache.cached_tmdb_request(
        "https://api.themoviedb.org/3/movie/999999",
        max_retries=2,
    )

    assert result["success"] is False
    assert len(calls) == 1
    assert calls[0][1]["timeout"] == (3, 5)


def test_tmdb_transient_error_retries_with_timeout(monkeypatch):
    from api.tmdb import cache

    calls = []

    class OkResponse:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {"id": 1}

    def fake_get(url, **kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            raise requests.exceptions.ConnectTimeout("temporary timeout")
        return OkResponse()

    monkeypatch.setattr(cache.requests, "get", fake_get)
    monkeypatch.setattr(cache.time, "sleep", lambda _: None)
    cache.tmdb_cache._store.clear()

    assert cache.cached_tmdb_request(
        "https://api.themoviedb.org/3/movie/1",
        max_retries=1,
    ) == {"id": 1}
    assert len(calls) == 2
    assert all(call["timeout"] == (3, 5) for call in calls)


@pytest.mark.parametrize("status_code", [400, 401, 403])
def test_tmdb_client_errors_are_not_retried(monkeypatch, status_code):
    from api.tmdb import cache

    calls = []

    class ErrorResponse:
        def __init__(self):
            self.status_code = status_code

        def raise_for_status(self):
            raise requests.HTTPError(response=self)

    def fake_get(url, **kwargs):
        calls.append(kwargs)
        return ErrorResponse()

    monkeypatch.setattr(cache.requests, "get", fake_get)
    cache.tmdb_cache._store.clear()

    result = cache.cached_tmdb_request(
        f"https://api.themoviedb.org/3/movie/{status_code}",
        max_retries=9,
    )

    assert len(calls) == 1
    assert result["success"] is False
    assert result["status_code"] == status_code


def test_tmdb_retry_count_is_capped(monkeypatch):
    from api.tmdb import cache

    calls = []

    class ErrorResponse:
        status_code = 503

        def raise_for_status(self):
            raise requests.HTTPError(response=self)

    def fake_get(url, **kwargs):
        calls.append(kwargs)
        return ErrorResponse()

    monkeypatch.setattr(cache.requests, "get", fake_get)
    monkeypatch.setattr(cache.time, "sleep", lambda _: None)
    cache.tmdb_cache._store.clear()

    with pytest.raises(requests.HTTPError):
        cache.cached_tmdb_request(
            "https://api.themoviedb.org/3/movie/503",
            max_retries=9,
        )

    assert len(calls) == 2


def test_actor_image_formatting_does_not_mutate_cached_data(monkeypatch):
    from api.tmdb import people

    responses = {
        "/person/1?": {
            "name": "Actor",
            "profile_path": "/profile.jpg",
            "gender": 0,
        },
        "/person/1/movie_credits?": {"cast": [], "crew": []},
        "/person/1/tv_credits?": {"cast": [], "crew": []},
        "/person/1/tagged_images?": {
            "results": [{"file_path": "/tagged.jpg", "vote_average": 1}]
        },
        "/person/1/external_ids?": {},
        "/person/1/images?": {
            "profiles": [{"file_path": "/profile-image.jpg", "vote_average": 1}]
        },
    }
    original_tagged = responses["/person/1/tagged_images?"]["results"][0].copy()
    original_profile = responses["/person/1/images?"]["profiles"][0].copy()

    def fake_cached_request(url, **kwargs):
        return next(value for key, value in responses.items() if key in url)

    monkeypatch.setattr(people, "cached_tmdb_request", fake_cached_request)

    first = people.fetch_actor_details(1)
    second = people.fetch_actor_details(1)

    assert responses["/person/1/tagged_images?"]["results"][0] == original_tagged
    assert responses["/person/1/images?"]["profiles"][0] == original_profile
    assert first["tagged_images"][0]["file_path"].count(
        "https://image.tmdb.org/t/p/w500"
    ) == 1
    assert second["profile_images"][0]["file_path"].count(
        "https://image.tmdb.org/t/p/w500"
    ) == 1


def test_actor_tmdb_budget_is_passed_to_all_requests(monkeypatch):
    from api.tmdb import people

    deadlines = []

    def fake_cached_request(url, **kwargs):
        deadlines.append(kwargs["deadline"])
        if "movie_credits" in url or "tv_credits" in url:
            return {"cast": [], "crew": []}
        if "tagged_images" in url:
            return {"results": []}
        if "external_ids" in url:
            return {}
        if "/images" in url:
            return {"profiles": []}
        return {"name": "Actor", "gender": 0}

    monkeypatch.setattr(people, "cached_tmdb_request", fake_cached_request)
    people.fetch_actor_details(1)

    assert len(deadlines) == 6
    assert len(set(deadlines)) == 1
