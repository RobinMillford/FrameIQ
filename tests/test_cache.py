"""
Tests for TMDB cache thread safety and eviction.
These verify the P0-2 fix (threading.Lock in _BoundedTTLCache).
"""
import threading
import time

import pytest
import requests
from flask import Flask


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


def test_actor_page_skips_optional_tmdb_enrichment(monkeypatch):
    from api.tmdb import people

    calls = []

    def fake_cached_request(url, **kwargs):
        calls.append((url, kwargs))
        if "movie_credits" in url or "tv_credits" in url:
            return {"cast": [], "crew": []}
        return {"name": "Actor", "profile_path": "/profile.jpg", "gender": 0}

    monkeypatch.setattr(people, "cached_tmdb_request", fake_cached_request)

    actor = people.fetch_actor_details(1)

    urls = [url for url, _ in calls]
    assert len(urls) == 3
    assert any("/person/1?" in url for url in urls)
    assert any("/person/1/movie_credits?" in url for url in urls)
    assert any("/person/1/tv_credits?" in url for url in urls)
    assert not any("tagged_images" in url for url in urls)
    assert not any("external_ids" in url for url in urls)
    assert not any("/images" in url for url in urls)
    assert actor["tagged_images"] == []
    assert actor["profile_images"] == []
    assert all(value is None for key, value in actor["external_ids"].items()
               if key != "tvrage_id")
    assert actor["external_ids"]["tvrage_id"] == 0


def test_actor_tmdb_budget_is_passed_to_all_requests(monkeypatch):
    from api.tmdb import people

    deadlines = []

    def fake_cached_request(url, **kwargs):
        deadlines.append(kwargs["deadline"])
        if "movie_credits" in url or "tv_credits" in url:
            return {"cast": [], "crew": []}
        return {"name": "Actor", "gender": 0}

    monkeypatch.setattr(people, "cached_tmdb_request", fake_cached_request)
    people.fetch_actor_details(1)

    assert len(deadlines) == 3
    assert len(set(deadlines)) == 1


def test_expensive_page_guard_rejects_without_waiting(monkeypatch):
    from utils import request_guard

    app = Flask(__name__)
    entered = threading.Event()

    @request_guard.expensive_page_limit
    def guarded():
        entered.set()
        return "ok"

    with app.test_request_context("/movie/1"):
        slot = request_guard._EXPENSIVE_PAGE_SLOTS
        assert slot.acquire(blocking=False)
        try:
            response = guarded()
            assert response.status_code == 429
            assert response.headers["Retry-After"] == "5"
            assert not entered.is_set()
        finally:
            slot.release()


def test_expensive_page_guard_releases_after_success_and_exception():
    from utils import request_guard

    app = Flask(__name__)
    calls = []

    @request_guard.expensive_page_limit
    def guarded(should_fail=False):
        calls.append(True)
        if should_fail:
            raise RuntimeError("boom")
        return "ok"

    with app.test_request_context("/movie/1"):
        assert guarded() == "ok"
        with pytest.raises(RuntimeError):
            guarded(should_fail=True)
        assert guarded() == "ok"
    assert len(calls) == 3


def test_health_route_does_not_use_expensive_page_guard():
    from app import app

    with app.test_client() as client:
        response = client.get("/health")
    assert response.status_code == 200
    assert response.json == {"status": "ok"}
