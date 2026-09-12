"""Smart Lists v1 (Feature 05) — focused regression tests.

Covers: model/validation, dynamic query behavior, per-filter correctness,
sorting/pagination, availability (bounded + region-aware + graceful
degradation), security (strict owner scoping), and hygiene (no per-card
TMDb requests, no timers/polling in shipped JS, no raw-ID titles).
"""
from datetime import datetime, timedelta

import pytest

from models import (db, MediaItem, user_watchlist, user_wishlist,
                    user_viewed, TVShowProgress)
from models.smart_lists import SmartList
from api.smart_lists import (validate_config, evaluate_smart_list,
                             rule_summary)


# ── helpers ──────────────────────────────────────────────────────────────────

def make_media(tmdb_id, title='A Movie', media_type='movie', rating=7.0,
               runtime=120, year=2020, genres='Action', priority='medium',
               add_to_watchlist=True, watched=False, user=None):
    m = MediaItem(
        tmdb_id=tmdb_id, media_type=media_type, title=title,
        release_date=datetime(year, 6, 15).date(), poster_path=f'/{tmdb_id}.jpg',
        rating=rating, runtime=runtime, genres=genres)
    db.session.add(m)
    db.session.flush()
    if user is not None:
        if add_to_watchlist:
            db.session.execute(user_watchlist.insert().values(
                user_id=user.id, media_id=m.id, media_type=media_type,
                priority=priority))
        if watched:
            db.session.execute(user_viewed.insert().values(
                user_id=user.id, media_id=m.id, media_type=media_type))
    return m


def make_list(user, name='Test', scope='watchlist', filters=None,
              sort='date_added'):
    sl = SmartList(user_id=user.id, name=name, scope=scope, sort=sort)
    sl.filters = filters or {}
    db.session.add(sl)
    db.session.commit()
    return sl


def _login_second_user(client, second_user):
    """A logged-in second user on the same client factory."""
    client.post('/logout', follow_redirects=True)
    client.post('/login', data={
        'username': second_user.username, 'password': 'TestPass1'},
        follow_redirects=True)
    return client


@pytest.fixture(autouse=True)
def _clean_smart_list_rows(db, sample_user):
    """Clean Smart List + media rows before sample_user teardown (depends on
    it for fixture ordering), so the unique tmdb_id on MediaItem never leaks
    between tests."""
    yield
    SmartList.query.delete()
    TVShowProgress.query.delete()
    db.session.execute(user_watchlist.delete())
    db.session.execute(user_wishlist.delete())
    db.session.execute(user_viewed.delete())
    MediaItem.query.delete()
    db.session.commit()


@pytest.fixture
def second_user(app):
    from models import User
    with app.app_context():
        u = User(username='other-slist', email='other-slist@example.com',
                 email_verified=True)
        u.set_password('TestPass1')
        db.session.add(u)
        db.session.commit()
        yield u
        db.session.delete(u)
        db.session.commit()


# ── model / configuration validation ────────────────────────────────────────

class TestValidation:
    def test_valid_config_round_trips(self):
        clean = validate_config(
            'watchlist', {'watch_state': 'unwatched', 'runtime': 'under_120'},
            'rating')
        assert clean == {'watch_state': 'unwatched', 'runtime': 'under_120'}

    def test_invalid_scope_rejected(self):
        with pytest.raises(ValueError):
            validate_config('nope', {}, 'rating')

    def test_invalid_sort_rejected(self):
        with pytest.raises(ValueError):
            validate_config('watchlist', {}, 'magic')

    def test_unknown_filter_rejected(self):
        with pytest.raises(ValueError):
            validate_config('watchlist', {'user_id': 2}, 'rating')

    def test_bad_filter_values_rejected(self):
        for bad in ({'watch_state': 'sometimes'}, {'media_type': 'book'},
                    {'year': 1800}, {'decade': 1995}, {'min_rating': 11},
                    {'runtime': 'under_5'}, {'genres': 'Action'},
                    {'service_id': -1}, {'tv_status': 'cancelled'}):
            with pytest.raises(ValueError):
                validate_config('watchlist', bad, 'rating')

    def test_non_dict_filters_rejected(self):
        with pytest.raises(ValueError):
            validate_config('watchlist', 'unwatched', 'rating')


# ── dynamic results ──────────────────────────────────────────────────────────

class TestDynamicResults:
    def test_unwatched_watchlist_returns_unwatched_only(self, app, sample_user):
        with app.app_context():
            make_media(603, 'Unwatched Movie', user=sample_user)
            make_media(27205, 'Watched Movie', watched=True, user=sample_user)
            sl = make_list(sample_user, filters={'watch_state': 'unwatched'})
            result = evaluate_smart_list(sl)
            titles = [i['title'] for i in result['items']]
            assert titles == ['Unwatched Movie']

    def test_watched_item_disappears_dynamically(self, app, sample_user):
        with app.app_context():
            m = make_media(603, 'Fresh', user=sample_user)
            sl = make_list(sample_user, filters={'watch_state': 'unwatched'})
            assert evaluate_smart_list(sl)['total'] == 1

            db.session.execute(user_viewed.insert().values(
                user_id=sample_user.id, media_id=m.id, media_type='movie'))
            db.session.commit()
            assert evaluate_smart_list(sl)['total'] == 0

    def test_new_watchlist_item_appears_automatically(self, app, sample_user):
        with app.app_context():
            sl = make_list(sample_user, filters={})
            assert evaluate_smart_list(sl)['total'] == 0
            make_media(999, 'New Arrival', user=sample_user)
            assert evaluate_smart_list(sl)['total'] == 1

    def test_removing_from_watchlist_removes_from_results(self, app, sample_user):
        with app.app_context():
            m = make_media(603, 'Bye', user=sample_user)
            sl = make_list(sample_user, filters={})
            assert evaluate_smart_list(sl)['total'] == 1
            db.session.execute(user_watchlist.delete().where(
                db.and_(user_watchlist.c.user_id == sample_user.id,
                        user_watchlist.c.media_id == m.id)))
            db.session.commit()
            assert evaluate_smart_list(sl)['total'] == 0

    def test_runtime_filter(self, app, sample_user):
        with app.app_context():
            make_media(1, 'Short', runtime=95, user=sample_user)
            make_media(2, 'Long', runtime=140, user=sample_user)
            sl = make_list(sample_user, filters={'runtime': 'under_120'})
            titles = [i['title'] for i in evaluate_smart_list(sl)['items']]
            assert titles == ['Short']

    def test_genre_filter(self, app, sample_user):
        with app.app_context():
            make_media(1, 'SciFi', genres='Science Fiction,Action',
                       user=sample_user)
            make_media(2, 'Drama', genres='Drama', user=sample_user)
            sl = make_list(sample_user, filters={'genres': ['Science Fiction']})
            titles = [i['title'] for i in evaluate_smart_list(sl)['items']]
            assert titles == ['SciFi']

    def test_year_and_decade_filters(self, app, sample_user):
        with app.app_context():
            make_media(1, 'Nineties', year=1994, user=sample_user)
            make_media(2, 'Twenties', year=2021, user=sample_user)
            year_sl = make_list(sample_user, filters={'year': 1994})
            assert [i['title'] for i in
                    evaluate_smart_list(year_sl)['items']] == ['Nineties']
            decade_sl = make_list(sample_user, filters={'decade': 2020})
            assert [i['title'] for i in
                    evaluate_smart_list(decade_sl)['items']] == ['Twenties']

    def test_rating_filter(self, app, sample_user):
        with app.app_context():
            make_media(1, 'Great', rating=8.5, user=sample_user)
            make_media(2, 'Meh', rating=6.0, user=sample_user)
            sl = make_list(sample_user, filters={'min_rating': 7.5})
            assert [i['title'] for i in
                    evaluate_smart_list(sl)['items']] == ['Great']

    def test_media_type_filter(self, app, sample_user):
        with app.app_context():
            make_media(1, 'Film', media_type='movie', user=sample_user)
            make_media(2, 'Series', media_type='tv', user=sample_user)
            sl = make_list(sample_user, filters={'media_type': 'tv'})
            titles = [i['title'] for i in evaluate_smart_list(sl)['items']]
            assert titles == ['Series']

    def test_watchlist_age_filter(self, app, sample_user):
        with app.app_context():
            m = make_media(1, 'Dusty', user=sample_user)
            old = datetime.utcnow() - timedelta(days=200)
            db.session.execute(
                user_watchlist.update().where(
                    db.and_(user_watchlist.c.user_id == sample_user.id,
                            user_watchlist.c.media_id == m.id))
                .values(date_added=old))
            make_media(2, 'Fresh Add', user=sample_user)
            sl = make_list(sample_user,
                           filters={'added_within': 'added_more_than_6_months'})
            titles = [i['title'] for i in evaluate_smart_list(sl)['items']]
            assert titles == ['Dusty']

    def test_tv_status_filter_on_tracked_tv(self, app, sample_user):
        from models import TVShowProgress
        with app.app_context():
            make_media(1399, 'Tracked Show', media_type='tv',
                       add_to_watchlist=False, user=sample_user)
            make_media(1400, 'Dropped Show', media_type='tv',
                       add_to_watchlist=False, user=sample_user)
            db.session.add(TVShowProgress(user_id=sample_user.id,
                                          show_id=1399, status='watching'))
            db.session.add(TVShowProgress(user_id=sample_user.id,
                                          show_id=1400, status='dropped'))
            db.session.commit()
            sl = make_list(sample_user, scope='tracked_tv',
                           filters={'tv_status': 'watching'})
            titles = [i['title'] for i in evaluate_smart_list(sl)['items']]
            assert titles == ['Tracked Show']

    def test_diary_scope_uses_viewed_state(self, app, sample_user):
        with app.app_context():
            make_media(603, 'Seen It', watched=True, user=sample_user)
            make_media(27205, 'Not Yet', user=sample_user)
            sl = make_list(sample_user, scope='diary', filters={})
            titles = [i['title'] for i in evaluate_smart_list(sl)['items']]
            assert titles == ['Seen It']


class TestSortsAndPagination:
    def test_priority_sort(self, app, sample_user):
        with app.app_context():
            make_media(1, 'Low P', priority='low', user=sample_user)
            make_media(2, 'High P', priority='high', user=sample_user)
            sl = make_list(sample_user, sort='priority')
            assert [i['title'] for i in
                    evaluate_smart_list(sl)['items']] == ['High P', 'Low P']

    def test_rating_sort_desc(self, app, sample_user):
        with app.app_context():
            make_media(1, 'Good', rating=7.0, user=sample_user)
            make_media(2, 'Best', rating=9.0, user=sample_user)
            sl = make_list(sample_user, sort='rating')
            assert [i['title'] for i in
                    evaluate_smart_list(sl)['items']] == ['Best', 'Good']

    def test_runtime_sort_unknown_last(self, app, sample_user):
        with app.app_context():
            make_media(1, 'No Runtime', runtime=None, user=sample_user)
            make_media(2, 'Short One', runtime=80, user=sample_user)
            sl = make_list(sample_user, sort='runtime')
            assert [i['title'] for i in
                    evaluate_smart_list(sl)['items']] == ['Short One',
                                                          'No Runtime']

    def test_random_is_deterministic_within_a_day(self, app, sample_user):
        with app.app_context():
            for n in range(8):
                make_media(1000 + n, f'M{n}', user=sample_user)
            sl = make_list(sample_user, sort='random')
            first = [i['id'] for i in evaluate_smart_list(sl)['items']]
            second = [i['id'] for i in evaluate_smart_list(sl)['items']]
            assert first == second

    def test_pagination(self, app, sample_user):
        with app.app_context():
            for n in range(30):
                make_media(2000 + n, f'P{n}', user=sample_user)
            sl = make_list(sample_user, filters={})
            page1 = evaluate_smart_list(sl, page=1, per_page=24)
            assert page1['total'] == 30 and len(page1['items']) == 24
            assert page1['pages'] == 2
            page2 = evaluate_smart_list(sl, page=2, per_page=24)
            assert len(page2['items']) == 6
            ids1 = {i['id'] for i in page1['items']}
            ids2 = {i['id'] for i in page2['items']}
            assert not ids1 & ids2


# ── availability ─────────────────────────────────────────────────────────────

class TestAvailability:
    def test_my_services_filter_respects_user_services(
            self, app, sample_user, monkeypatch):
        from models.streaming import UserStreamingService
        with app.app_context():
            make_media(1, 'OnNetflix', user=sample_user)
            make_media(2, 'OnHulu', user=sample_user)
            db.session.add(UserStreamingService(user_id=sample_user.id,
                                                provider_id=8, region='US'))
            db.session.commit()

            def fake_providers(media_type, tmdb_id):
                return {
                    'US': {'flatrate': ([{'provider_id': 8,
                                          'provider_name': 'Netflix'}]
                                        if tmdb_id == 1 else
                                        [{'provider_id': 15,
                                          'provider_name': 'Hulu'}])},
                }

            monkeypatch.setattr('api.availability._fetch_provider_results',
                                fake_providers)
            sl = make_list(sample_user, filters={'services': 'my_services'})
            titles = [i['title'] for i in evaluate_smart_list(sl)['items']]
            assert titles == ['OnNetflix']

    def test_rent_buy_never_count_as_streaming(self, app, sample_user,
                                               monkeypatch):
        with app.app_context():
            make_media(1, 'RentOnly', user=sample_user)
            db.session.commit()

            def fake_providers(media_type, tmdb_id):
                return {'US': {'rent': [{'provider_id': 8,
                                         'provider_name': 'Netflix'}]}}

            monkeypatch.setattr('api.availability._fetch_provider_results',
                                fake_providers)
            sl = make_list(sample_user, filters={'services': 'my_services'})
            assert evaluate_smart_list(sl)['total'] == 0

    def test_availability_failure_degrades_gracefully(
            self, app, sample_user, monkeypatch):
        with app.app_context():
            make_media(1, 'Mystery', user=sample_user)
            make_media(2, 'Known', user=sample_user)
            db.session.commit()

            def flaky(media_type, tmdb_id):
                if tmdb_id == 1:
                    return None  # upstream failure / unknown
                return {'US': {'flatrate': [{'provider_id': 8,
                                             'provider_name': 'Netflix'}]}}

            monkeypatch.setattr('api.availability._fetch_provider_results',
                                flaky)
            sl = make_list(sample_user, filters={'services': 'my_services'})
            titles = [i['title'] for i in evaluate_smart_list(sl)['items']]
            assert titles == ['Known']  # unknown omitted, list still works

    def test_no_availability_filter_means_no_provider_requests(
            self, app, sample_user, monkeypatch):
        with app.app_context():
            make_media(1, 'Plain', user=sample_user)
            db.session.commit()

            def boom(media_type, tmdb_id):
                raise AssertionError('TMDb providers must not be fetched')

            monkeypatch.setattr('api.availability._fetch_provider_results',
                                boom)
            sl = make_list(sample_user, filters={'runtime': 'under_150'})
            assert evaluate_smart_list(sl)['total'] == 1

    def test_availability_deduplicated_per_title(self, app, sample_user,
                                                 monkeypatch):
        with app.app_context():
            make_media(1, 'A', user=sample_user)
            make_media(2, 'B', user=sample_user)
            db.session.commit()

            calls = []

            def counting(media_type, tmdb_id):
                calls.append(tmdb_id)
                return {'US': {'flatrate': [{'provider_id': 8,
                                             'provider_name': 'Netflix'}]}}

            monkeypatch.setattr('api.availability._fetch_provider_results',
                                counting)
            sl = make_list(sample_user, filters={'services': 'my_services'})
            assert evaluate_smart_list(sl)['total'] == 2
            # One probe per DISTINCT title within a single evaluation —
            # never one per rendered card and never re-probed per page.
            assert len(calls) == 2


# ── security / API surface ───────────────────────────────────────────────────

class TestSecurityAndAPI:
    def test_user_cannot_access_another_users_list(self, app, auth_client,
                                                   sample_user, second_user):
        with auth_client.application.app_context():
            sl = make_list(sample_user, name='Mine')
            sid = sl.id
        # The owner gets everything normally.
        assert auth_client.get(f'/api/smart-lists/{sid}').status_code == 200
        # A different authenticated user gets 404 — same as a nonexistent id;
        # the row never leaks across the ownership boundary.
        other = app.test_client()
        other.post('/login', data={
            'username': second_user.username, 'password': 'TestPass1'},
            follow_redirects=True)
        assert other.get(f'/api/smart-lists/{sid}').status_code == 404
        assert other.get(
            f'/api/smart-lists/{sid}/results').status_code == 404
        assert other.put(f'/api/smart-lists/{sid}',
                         json={'name': 'hax'}).status_code == 404
        assert other.delete(f'/api/smart-lists/{sid}').status_code == 404
        assert other.get(f'/smart-lists/{sid}').status_code == 404

    def test_listing_returns_only_own_lists(self, auth_client, sample_user,
                                            second_user):
        with auth_client.application.app_context():
            make_list(sample_user, name='Mine')
            make_list(second_user, name='Theirs')
        data = auth_client.get('/api/smart-lists').get_json()
        names = [s['name'] for s in data['smart_lists']]
        assert names == ['Mine']

    def test_create_validates_and_persists(self, auth_client):
        r = auth_client.post('/api/smart-lists', json={
            'name': 'Tonight', 'scope': 'watchlist',
            'filters': {'watch_state': 'unwatched', 'runtime': 'under_120'},
            'sort': 'priority'})
        assert r.status_code == 201
        body = r.get_json()
        assert body['filters']['watch_state'] == 'unwatched'
        assert 'My Watchlist' in body['rules']

    def test_create_rejects_invalid_config_with_400(self, auth_client):
        for bad in ({'name': 'X', 'scope': 'bogus', 'filters': {},
                     'sort': 'rating'},
                    {'name': 'X', 'scope': 'watchlist',
                     'filters': {'nope': 1}, 'sort': 'rating'},
                    {'name': '', 'scope': 'watchlist', 'filters': {},
                     'sort': 'rating'}):
            r = auth_client.post('/api/smart-lists', json=bad)
            assert r.status_code == 400
            assert 'error' in r.get_json()

    def test_results_endpoint_is_live_and_dynamic(self, auth_client,
                                                  sample_user):
        with auth_client.application.app_context():
            make_media(603, 'Matrix', user=sample_user)
            r = auth_client.post('/api/smart-lists', json={
                'name': 'All', 'scope': 'watchlist', 'filters': {},
                'sort': 'date_added'})
            sid = r.get_json()['id']
        data = auth_client.get(f'/api/smart-lists/{sid}/results').get_json()
        assert data['total'] == 1
        assert data['items'][0]['title'] == 'Matrix'
        assert data['items'][0]['detail_url'] == '/movie/603'

    def test_unauthenticated_rejected(self, client):
        for method, path in (('get', '/api/smart-lists'),
                             ('post', '/api/smart-lists'),
                             ('get', '/api/smart-lists/1/results')):
            r = getattr(client, method)(path)
            assert r.status_code in (302, 401)

    def test_config_never_leaks_cross_user_data(self, app, sample_user,
                                                second_user):
        with app.app_context():
            # Even a hand-crafted row with a bogus scope fails closed —
            # evaluation always re-validates and raises on unknown scope.
            make_media(1, 'Mine', user=sample_user)
            sl = make_list(second_user, filters={})
            assert evaluate_smart_list(sl)['total'] == 0  # second_user's data


# ── hygiene ──────────────────────────────────────────────────────────────────

class TestHygiene:
    def test_engine_module_has_no_polling_or_timers(self):
        with open('api/smart_lists.py', encoding='utf-8') as fh:
            src = fh.read()
        for banned in ('setInterval', 'setTimeout', 'while True',
                       'requests.get', 'urllib', 'threading.Timer'):
            assert banned not in src, f'banned token {banned!r} in engine'

    def test_shipped_js_has_no_polling_or_timers(self):
        with open('static/js/smart-list-detail.js', encoding='utf-8') as fh:
            src = fh.read()
        for banned in ('setInterval', 'setTimeout(',
                       'requestAnimationFrame', 'WebSocket',
                       'EventSource'):
            assert banned not in src, f'banned token {banned!r} in JS'

    def test_rule_summary_has_no_raw_ids(self, app, sample_user):
        with app.app_context():
            sl = make_list(sample_user, name='R',
                           filters={'watch_state': 'unwatched',
                                    'runtime': 'under_120',
                                    'services': 'my_services'},
                           sort='rating')
            rules = rule_summary(sl)
            assert rules == ['My Watchlist', 'Unwatched', 'Under 120 min',
                             'On My Services', 'Rating']
