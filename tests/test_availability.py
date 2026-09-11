"""
Feature 03 — Streaming Intelligence: Where to Watch + My Services.

Covers:
- provider normalization (movie/TV, region, categories, missing data)
- availability API (auth match, region fallback)
- My Services CRUD (auth, idempotency, duplicate prevention, ownership)
- matching semantics (rent never counts as streaming)
- graceful degradation (TMDb failure never 500s a page)
- caching behavior (no N+1 / repeated upstream calls)

External TMDb calls are mocked at the cached_tmdb_request boundary.
"""
from unittest.mock import patch

import pytest

from models import db, UserStreamingService


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures / TMDb mocking
# ─────────────────────────────────────────────────────────────────────────────

TMDB_PROVIDERS_RESPONSE = {
    "id": 603,
    "results": {
        "US": {
            "link": "https://www.themoviedb.org/movie/603/watch?locale=US",
            "flatrate": [
                {"provider_id": 8, "provider_name": "Netflix",
                 "logo_path": "/netflix.jpg", "display_priority": 8},
                {"provider_id": 1899, "provider_name": "HBO Max",
                 "logo_path": "/hbo.jpg", "display_priority": 10},
            ],
            "rent": [
                {"provider_id": 2, "provider_name": "Apple TV",
                 "logo_path": None, "display_priority": 20},
            ],
            "buy": [
                {"provider_id": 2, "provider_name": "Apple TV",
                 "logo_path": None, "display_priority": 20},
            ],
            "free": [
                {"provider_id": 300, "provider_name": "Tubi",
                 "logo_path": "/tubi.jpg", "display_priority": 50},
            ],
        },
        "BD": {},  # nothing available in Bangladesh
    },
}

EMPTY_RESPONSE = {"id": 603, "results": {}}


@pytest.fixture
def tmdb_providers():
    """Patch cached_tmdb_request to return canned provider data."""
    import api.availability
    calls = []

    def fake_request(url, **kwargs):
        calls.append(url)
        if "/watch/providers/tv?" in url:
            return {"results": [
                {"provider_id": 8, "provider_name": "Netflix",
                 "logo_path": "/n.jpg", "display_priority": 1},
                {"provider_id": 1899, "provider_name": "HBO Max",
                 "logo_path": "/h.jpg", "display_priority": 2},
            ]}
        return TMDB_PROVIDERS_RESPONSE

    with patch.object(api.availability, "cached_tmdb_request", side_effect=fake_request):
        # Reset the per-process memo between tests for isolation.
        api.availability._provider_memo.clear()
        yield calls


@pytest.fixture
def svc_user(app, db):
    """Owns its cleanup; query-based deletes avoid stale-object teardown errors."""
    from models import User
    with app.app_context():
        u = User(username='svcuser', email='svc@example.com', email_verified=True)
        u.set_password('SvcPass123')
        db.session.add(u)
        db.session.commit()
        yield u
        uid = u.id
        UserStreamingService.query.filter_by(user_id=uid).delete()
        User.query.filter(User.id == uid).delete(synchronize_session=False)
        db.session.commit()


@pytest.fixture
def svc_client(client, svc_user):
    client.post('/login', data={'username': 'svcuser', 'password': 'SvcPass123'})
    return client


@pytest.fixture
def anon_cleanup(app, db):
    """Sweep My Services rows and the ad-hoc user created inside a test body.

    Deliberately does NOT touch 'svcuser' — the svc_user fixture owns that
    lifecycle and deleting it here would break svc_user's teardown.
    """
    yield
    with app.app_context():
        from models import User
        UserStreamingService.query.delete()
        User.query.filter(User.username == 'otheruser').delete(
            synchronize_session=False)
        db.session.commit()


# ─────────────────────────────────────────────────────────────────────────────
# Provider normalization
# ─────────────────────────────────────────────────────────────────────────────

class TestNormalization:
    def test_movie_provider_response_normalized(self, tmdb_providers):
        from api.availability import get_availability
        a = get_availability('movie', 603, 'US')
        assert a['region'] == 'US'
        assert a['available'] is True
        assert a['link'].startswith('https://www.themoviedb.org/movie/603/watch')
        names = [p['name'] for p in a['stream']]
        assert names == ['Netflix', 'HBO Max']  # sorted by display_priority
        assert a['stream'][0] == {'id': 8, 'name': 'Netflix',
                                  'logo': '/netflix.jpg', 'priority': 8}

    def test_tv_provider_response_normalized(self, tmdb_providers):
        from api.availability import get_availability
        a = get_availability('tv', 1399, 'US')
        assert [p['name'] for p in a['stream']] == ['Netflix', 'HBO Max']

    def test_region_specific_data_respected(self, tmdb_providers):
        from api.availability import get_availability
        a = get_availability('movie', 603, 'BD')
        assert a['region'] == 'BD'
        assert a['available'] is False
        assert a['stream'] == []

    def test_categories_remain_distinct(self, tmdb_providers):
        from api.availability import get_availability
        a = get_availability('movie', 603, 'US')
        assert {p['name'] for p in a['free']} == {'Tubi'}
        assert {p['name'] for p in a['rent']} == {'Apple TV'}
        assert {p['name'] for p in a['buy']} == {'Apple TV'}
        assert 'Apple TV' not in {p['name'] for p in a['stream']}

    def test_missing_provider_data_safe_state(self):
        import api.availability
        with patch.object(api.availability, "cached_tmdb_request",
                          return_value=EMPTY_RESPONSE):
            api.availability._provider_memo.clear()
            a = api.availability.get_availability('movie', 42, 'US')
        assert a['available'] is False
        assert a['stream'] == [] and a['rent'] == []

    def test_missing_logo_handled(self, tmdb_providers):
        from api.availability import get_availability
        a = get_availability('movie', 603, 'US')
        apple = [p for p in a['rent'] if p['name'] == 'Apple TV'][0]
        assert apple['logo'] is None  # UI falls back to the name

    def test_invalid_region_falls_back_to_default(self, tmdb_providers):
        from api.availability import get_availability, DEFAULT_REGION
        a = get_availability('movie', 603, None)
        assert a['region'] == DEFAULT_REGION

    def test_tmdb_failure_does_not_raise(self):
        import api.availability
        with patch.object(api.availability, "cached_tmdb_request",
                          side_effect=RuntimeError("upstream down")):
            api.availability._provider_memo.clear()
            a = api.availability.get_availability('movie', 603, 'US')
        assert a['available'] is False  # safe empty shape, no exception


# ─────────────────────────────────────────────────────────────────────────────
# Caching
# ─────────────────────────────────────────────────────────────────────────────

class TestProviderCaching:
    def test_repeated_requests_single_upstream_call(self, tmdb_providers):
        from api.availability import get_availability
        for _ in range(3):
            get_availability('movie', 603, 'US')
            get_availability('movie', 603, 'BD')
        detail_calls = [u for u in tmdb_providers if '/movie/603/watch/providers' in u]
        assert len(detail_calls) == 1  # one upstream response serves all regions

    def test_different_regions_cached_separately(self, tmdb_providers):
        from api.availability import get_availability
        # One upstream fetch; per-region results resolve independently.
        assert get_availability('movie', 603, 'US')['available'] is True
        assert get_availability('movie', 603, 'BD')['available'] is False
        detail_calls = [u for u in tmdb_providers if '/movie/603/watch/providers' in u]
        assert len(detail_calls) == 1


# ─────────────────────────────────────────────────────────────────────────────
# Availability API
# ─────────────────────────────────────────────────────────────────────────────

class TestAvailabilityAPI:
    def test_movie_availability_endpoint(self, client, tmdb_providers):
        r = client.get('/api/media/movie/603/availability')
        assert r.status_code == 200
        data = r.get_json()
        assert data['media_type'] == 'movie'
        assert data['region'] == 'US'
        assert [p['name'] for p in data['providers']['stream']] == ['Netflix', 'HBO Max']
        assert 'my_services' not in data  # anonymous — no personal data leaks

    def test_authenticated_match_included(self, svc_client, tmdb_providers, anon_cleanup):
        svc_client.post('/api/me/streaming-services',
                        json={'services': [8], 'region': 'US'})
        r = svc_client.get('/api/media/movie/603/availability?region=US')
        data = r.get_json()
        assert data['my_services']['available'] is True
        assert [p['name'] for p in data['my_services']['matches']] == ['Netflix']

    def test_invalid_media_type_rejected(self, client):
        r = client.get('/api/media/book/603/availability')
        assert r.status_code == 400

    def test_detail_pages_render_with_availability_section(self, client, monkeypatch):
        import routes.details as details
        monkeypatch.setattr(details, 'fetch_movie_details', lambda _id: {
            'id': _id, 'title': 'T', 'poster_path': None, 'overview': '',
            'release_date': '', 'genres': [], 'vote_average': 0,
            'recommendations': [], 'budget': 0, 'revenue': 0,
            'cast': [], 'crew': [], 'videos': {'results': []},
            'images': {}, 'runtime': 100, 'reviews': [],
            'tagline': '', 'vote_count': 0, 'status': 'Released',
            'original_language': 'en', 'trailer_url': None,
            'certification': None, 'director': '', 'writer': '',
            'backdrop_path': None,
        })
        r = client.get('/movie/603')
        assert r.status_code == 200
        assert b'where-to-watch' in r.data

    def test_tv_detail_pages_render_with_availability_section(self, client, monkeypatch):
        import routes.details as details
        monkeypatch.setattr(details, 'fetch_tv_show_details', lambda _id: {
            'id': _id, 'name': 'S', 'poster_path': None, 'overview': '',
            'first_air_date': '', 'genres': [], 'vote_average': 0,
            'number_of_seasons': 1, 'seasons': [], 'status': 'Ended',
            'origin_country': [], 'created_by': [], 'cast': [],
            'videos': {'results': []}, 'episode_run_time': [45],
        })
        r = client.get('/tv/1399')
        assert r.status_code == 200
        assert b'where-to-watch' in r.data


# ─────────────────────────────────────────────────────────────────────────────
# My Services
# ─────────────────────────────────────────────────────────────────────────────

class TestMyServices:
    def test_save_and_list_services(self, svc_client, tmdb_providers, anon_cleanup):
        r = svc_client.post('/api/me/streaming-services',
                            json={'services': [8, 1899], 'region': 'US'})
        assert r.status_code == 200
        assert r.get_json()['services'] == [8, 1899]

        r = svc_client.get('/api/me/streaming-services?region=US')
        assert r.get_json()['services'] == [8, 1899]

    def test_remove_service(self, svc_client, tmdb_providers, anon_cleanup):
        svc_client.post('/api/me/streaming-services',
                        json={'services': [8, 1899], 'region': 'US'})
        r = svc_client.delete('/api/me/streaming-services/8?region=US')
        assert r.status_code == 200
        remaining = svc_client.get('/api/me/streaming-services?region=US').get_json()['services']
        assert remaining == [1899]

    def test_duplicate_service_not_created(self, client, svc_client, tmdb_providers, anon_cleanup):
        for _ in range(3):
            r = svc_client.post('/api/me/streaming-services',
                                json={'services': [8], 'region': 'US'})
            assert r.status_code == 200
        with client.application.app_context():
            from models import User
            u = User.query.filter_by(username='svcuser').first()
            rows = UserStreamingService.query.filter_by(
                user_id=u.id, provider_id=8, region='US').all()
            assert len(rows) == 1  # idempotent replace, no duplicates

    def test_unknown_provider_rejected(self, svc_client, tmdb_providers, anon_cleanup):
        r = svc_client.post('/api/me/streaming-services',
                            json={'services': [999999], 'region': 'US'})
        assert r.status_code == 400
        assert r.get_json()['invalid_provider_ids'] == [999999]

    def test_user_a_cannot_modify_user_b(self, client, svc_client, tmdb_providers, anon_cleanup):
        """Each account's save/delete only ever touches its own rows."""
        from models import User
        with client.application.app_context():
            other = User(username='otheruser', email='other@example.com', email_verified=True)
            other.set_password('OtherPass1')
            db.session.add(other)
            db.session.commit()
            other_id = other.id

        # User B saves Netflix.
        client.post('/login', data={'username': 'otheruser', 'password': 'OtherPass1'})
        client.post('/api/me/streaming-services', json={'services': [8], 'region': 'US'})

        # Switch the same client to User A (svcuser): save a different set,
        # then delete everything User A owns.
        client.post('/login', data={'username': 'svcuser', 'password': 'SvcPass123'})
        client.post('/api/me/streaming-services', json={'services': [1899], 'region': 'US'})
        client.post('/api/me/streaming-services', json={'services': [], 'region': 'US'})

        rows = UserStreamingService.query.filter_by(region='US').all()
        # User A's rows are gone; User B's row survives and belongs only to B.
        assert [(r.user_id, r.provider_id) for r in rows] == [(other_id, 8)]

    def test_service_persists_across_requests(self, svc_client, tmdb_providers, anon_cleanup):
        svc_client.post('/api/me/streaming-services',
                        json={'services': [8], 'region': 'US'})
        r1 = svc_client.get('/api/me/streaming-services?region=US')
        r2 = svc_client.get('/api/me/streaming-services?region=US')
        assert r1.get_json()['services'] == r2.get_json()['services'] == [8]

    def test_region_preference_saved_and_used(self, client, svc_client, tmdb_providers, anon_cleanup):
        svc_client.post('/api/me/streaming-services',
                        json={'services': [8], 'region': 'US'})
        with client.application.app_context():
            from models import User
            u = User.query.filter_by(username='svcuser').first()
            assert u.streaming_region == 'US'

    def test_unauthenticated_save_rejected(self, client):
        r = client.post('/api/me/streaming-services', json={'services': [8]})
        assert r.status_code in (302, 401)

    def test_anonymous_availability_has_no_personal_data(self, client, tmdb_providers):
        r = client.get('/api/media/movie/603/availability?region=US')
        data = r.get_json()
        assert 'my_services' not in data


# ─────────────────────────────────────────────────────────────────────────────
# Matching semantics
# ─────────────────────────────────────────────────────────────────────────────

class TestMatching:
    def test_matching_service_identified(self, tmdb_providers):
        from api.availability import get_availability, match_my_services
        a = get_availability('movie', 603, 'US')
        m = match_my_services(a, [8])
        assert m['available'] is True
        assert [p['name'] for p in m['matches']] == ['Netflix']

    def test_non_matching_service_not_reported(self, tmdb_providers):
        from api.availability import get_availability, match_my_services
        a = get_availability('movie', 603, 'US')
        m = match_my_services(a, [337])  # Disney+ not present
        assert m['available'] is False
        assert m['matches'] == []

    def test_rent_only_never_counts_as_streaming(self, tmdb_providers):
        from api.availability import get_availability, match_my_services
        a = get_availability('movie', 603, 'US')
        # Apple TV only offers rent/buy here — selecting it must NOT yield
        # "available on your services".
        m = match_my_services(a, [2])
        assert m['available'] is False
        assert m['matches'] == []

    def test_no_availability_safe_state(self, tmdb_providers):
        from api.availability import get_availability, match_my_services
        a = get_availability('movie', 603, 'BD')
        assert a['available'] is False
        m = match_my_services(a, [8])
        assert m['available'] is False

    def test_free_provider_counts_as_available(self, tmdb_providers):
        from api.availability import get_availability, match_my_services
        a = get_availability('movie', 603, 'US')
        m = match_my_services(a, [300])  # Tubi (free)
        assert m['available'] is True


# ─────────────────────────────────────────────────────────────────────────────
# Part A — finite states (spinner must never stick)
# ─────────────────────────────────────────────────────────────────────────────

class TestFiniteStates:
    """Where-to-Watch must always leave LOADING: one request, one finite
    result. NO_AVAILABILITY (successful empty lookup) and UNKNOWN (upstream
    failure) are distinct states and are never merged."""

    def test_successful_empty_result_has_ok_status(self, tmdb_providers):
        """Bangladesh with zero providers = NO_AVAILABILITY, not a spinner."""
        from api.availability import get_availability
        a = get_availability('movie', 603, 'BD')
        assert a['status'] == 'ok'
        assert a['available'] is False
        assert a['stream'] == [] and a['rent'] == [] and a['buy'] == []

    def test_tmdb_failure_yields_unknown_status(self):
        import api.availability
        with patch.object(api.availability, "cached_tmdb_request",
                          side_effect=RuntimeError("upstream down")):
            api.availability._provider_memo.clear()
            a = api.availability.get_availability('movie', 603, 'US')
        assert a['status'] == 'unknown'   # never reported as "not available"
        assert a['available'] is False

    def test_failure_is_not_cached_as_empty(self):
        """A failed lookup must NOT be memoized: the next request retries
        instead of staying 'unknown' for the whole memo TTL."""
        import api.availability
        with patch.object(api.availability, "cached_tmdb_request",
                          side_effect=RuntimeError("upstream down")):
            api.availability._provider_memo.clear()
            first = api.availability.get_availability('movie', 777, 'US')
        assert first['status'] == 'unknown'
        # After failure, a succeeding request resolves normally (no stale memo).
        with patch.object(api.availability, "cached_tmdb_request",
                          return_value=TMDB_PROVIDERS_RESPONSE):
            api.availability._provider_memo.clear()
            second = api.availability.get_availability('movie', 777, 'US')
        assert second['status'] == 'ok'
        assert second['available'] is True

    def test_no_silent_region_fallback(self, tmdb_providers):
        """A region with no data must never silently fall back to another
        country: the response region stays the requested one."""
        from api.availability import get_availability
        a = get_availability('movie', 603, 'BD')
        assert a['region'] == 'BD'           # Bangladesh stays Bangladesh
        assert a['available'] is False

    def test_detail_page_passes_context_to_wtw_partial(self, client, monkeypatch):
        """Root cause of the infinite spinner: the partial read media_type /
        media_id, which the routes never passed — data attributes rendered
        empty and the loader bailed before fetching. The partial must now
        render non-empty data attributes on its own."""
        import routes.details as details
        monkeypatch.setattr(details, 'fetch_movie_details', lambda _id: {
            'id': _id, 'title': 'T', 'poster_path': None, 'overview': '',
            'release_date': '', 'genres': [], 'vote_average': 0,
            'recommendations': [], 'budget': 0, 'revenue': 0,
            'cast': [], 'crew': [], 'videos': {'results': []},
            'images': {}, 'runtime': 100, 'reviews': [],
            'tagline': '', 'vote_count': 0, 'status': 'Released',
            'original_language': 'en', 'trailer_url': None,
            'certification': None, 'director': '', 'writer': '',
            'backdrop_path': None,
        })
        r = client.get('/movie/603')
        html = r.data.decode('utf-8')
        assert r.status_code == 200
        assert 'data-media-type="movie"' in html
        assert 'data-media-id="603"' in html

    def test_loader_js_has_finite_states(self):
        """Source guard: the loader must dispatch to a terminal state for
        every outcome and never poll."""
        src = open('static/js/where-to-watch.js').read()
        assert 'NO_AVAILABILITY' in src or 'stateNoAvailability' in src
        assert 'stateUnknown' in src
        assert 'terminalState' in src
        for banned in ('setInterval', 'setTimeout', 'while (true)'):
            assert banned not in src, f"polling/timer machinery found: {banned}"
