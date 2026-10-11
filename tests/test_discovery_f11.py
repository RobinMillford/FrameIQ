"""F11 — Discovery facets + saved discovery presets.

Covers: the facet validation contract (per-facet, combined, media-correct date
semantics, sorting, bounded pagination), URL round-tripping, the /explore page
and the proxy parameters it emits, saved-preset storage/CRUD/ownership, strict
isolation of presets from the SQL-first Smart List evaluator, and security
(CSRF, ownership 404s, no provider-availability claims).

Everything runs offline: TMDb responses come from the shared registry fixture.
"""
import pytest

from api.discovery import (
    DISCOVERY_KIND, DiscoveryError, build_discover_params, clamp_page,
    config_to_query, discover_path, genres_for, parse_discovery_args,
    sorts_for, summarize, validate_discovery_config,
)
from api.smart_lists import (
    evaluate_smart_list, is_discovery_preset, rule_summary, validate_config,
)
from models import db
from models.smart_lists import SmartList


# ── helpers ────────────────────────────────────────────────────────────────

def _config(**kw):
    kw.setdefault('media_type', 'movie')
    return validate_discovery_config(kw)


def _user(db, name):
    from models import User
    u = User(username=name, email=f'{name}@example.test')
    u.set_password('pw-for-tests-only')
    db.session.add(u)
    db.session.commit()
    return u


def _make_preset(user, name='Sci-fi', config=None, scope='all',
                 sort='date_added'):
    config = config if config is not None else _config(genres=['science_fiction'])
    preset = SmartList(
        user_id=user.id, name=name, scope=scope, sort=sort,
        filters={'kind': DISCOVERY_KIND, 'discovery': config},
        is_public=False)
    db.session.add(preset)
    db.session.commit()
    return preset


def _make_normal_list(user, name='My watchlist picks'):
    sl = SmartList(
        user_id=user.id, name=name, scope='watchlist', sort='date_added',
        filters={'min_rating': 6}, is_public=False)
    db.session.add(sl)
    db.session.commit()
    return sl


# ── Facets: individual ─────────────────────────────────────────────────────

class TestFacetValidation:

    def test_genre_alone_is_accepted_for_each_supported_genre(self):
        for name in genres_for('movie'):
            assert _config(genres=[name])['genres'] == [name]

    def test_year_range_is_accepted(self):
        cfg = _config(year_from=1990, year_to=1999)
        assert (cfg['year_from'], cfg['year_to']) == (1990, 1999)

    def test_single_year_is_accepted(self):
        assert _config(year_from=2001)['year_from'] == 2001

    def test_min_rating_is_accepted_and_rounded(self):
        assert _config(min_rating=7.26)['min_rating'] == 7.3

    def test_media_type_is_canonicalised(self):
        cfg = validate_discovery_config({'media_type': ' TV '})
        assert cfg['media_type'] == 'tv'

    @pytest.mark.parametrize('value', ['', '   ', 'not_a_genre', '9999'])
    def test_unknown_genre_is_rejected(self, value):
        with pytest.raises(DiscoveryError):
            _config(genres=[value])

    def test_genre_case_and_spacing_are_normalised(self):
        assert _config(genres=['Science Fiction'])['genres'] == ['science_fiction']

    def test_genres_must_be_a_list(self):
        with pytest.raises(DiscoveryError):
            _config(genres='horror')

    @pytest.mark.parametrize('bad', [-1, 11, 'abc', 5000])
    def test_out_of_range_or_unreadable_rating_is_rejected(self, bad):
        with pytest.raises(DiscoveryError):
            _config(min_rating=bad)

    @pytest.mark.parametrize('bad', [1800, 9999, 'nope'])
    def test_out_of_range_year_is_rejected(self, bad):
        with pytest.raises(DiscoveryError):
            _config(year_from=bad)

    def test_unsupported_media_type_is_rejected(self):
        with pytest.raises(DiscoveryError):
            validate_discovery_config({'media_type': 'anime'})

    def test_non_dict_config_is_rejected(self):
        with pytest.raises(DiscoveryError):
            validate_discovery_config(['genre'])

    def test_genre_cap_is_enforced(self):
        names = list(genres_for('movie'))[:10]
        assert _config(genres=names)['genres'] == names
        with pytest.raises(DiscoveryError):
            _config(genres=names + ['documentary', 'music'])


# ── Facets: combined ───────────────────────────────────────────────────────

class TestCombinedFacets:

    def test_all_supported_facets_combine(self):
        cfg = _config(media_type='movie', genres=['action', 'thriller'],
                      year_from=2000, year_to=2010, min_rating=7.5,
                      sort='vote_average.desc')
        assert cfg == {
            'media_type': 'movie', 'sort': 'vote_average.desc',
            'genres': ['action', 'thriller'], 'year_from': 2000,
            'year_to': 2010, 'min_rating': 7.5,
        }

    def test_genres_are_deduplicated_and_ordered_as_sent(self):
        cfg = _config(genres=['drama', 'drama', 'comedy'])
        assert cfg['genres'] == ['drama', 'comedy']

    def test_contradictory_range_is_normalised_by_swapping(self):
        cfg = _config(year_from=2010, year_to=2000)
        assert (cfg['year_from'], cfg['year_to']) == (2000, 2010)

    def test_defaults_are_minimal(self):
        assert _config() == {'media_type': 'movie', 'sort': 'popularity.desc'}

    def test_tv_genre_vocabulary_is_tv_specific(self):
        with pytest.raises(DiscoveryError):
            _config(media_type='tv', genres=['science_fiction'])
        assert _config(media_type='tv', genres=['sci_fi_fantasy'])

    def test_unrelated_facets_survive_an_unrelated_change(self):
        base = _config(genres=['horror'], year_from=1990, min_rating=6)
        changed = dict(base, min_rating=8)
        assert changed['genres'] == base['genres']
        assert changed['year_from'] == base['year_from']


# ── Media-specific date semantics ──────────────────────────────────────────

class TestMediaDateSemantics:

    def test_movie_uses_primary_release_date_fields(self):
        params = build_discover_params(_config(media_type='movie',
                                               year_from=1999, year_to=2001))
        assert params['primary_release_date.gte'] == '1999-01-01'
        assert params['primary_release_date.lte'] == '2001-12-31'
        assert 'first_air_date.gte' not in params

    def test_tv_uses_first_air_date_fields(self):
        params = build_discover_params(_config(media_type='tv',
                                               year_from=1999, year_to=2001))
        assert params['first_air_date.gte'] == '1999-01-01'
        assert params['first_air_date.lte'] == '2001-12-31'
        assert 'primary_release_date.gte' not in params

    def test_discover_path_follows_media_type(self):
        assert discover_path('movie') == '/discover/movie'
        assert discover_path('tv') == '/discover/tv'

    def test_tv_rejects_movie_only_sort(self):
        with pytest.raises(DiscoveryError):
            _config(media_type='tv', sort='primary_release_date.desc')

    def test_movie_and_tv_offer_different_date_sorts(self):
        assert 'first_air_date.desc' in sorts_for('tv')
        assert 'first_air_date.desc' not in sorts_for('movie')
        assert 'primary_release_date.desc' in sorts_for('movie')


# ── Parameters, sorting, pagination ────────────────────────────────────────

class TestTmdbParameters:

    def test_genres_become_tmdb_ids(self):
        params = build_discover_params(_config(genres=['action', 'comedy']))
        assert params['with_genres'] == '28,35'

    def test_rating_becomes_vote_average_gte(self):
        params = build_discover_params(_config(min_rating=6.5))
        assert params['vote_average.gte'] == 6.5

    def test_sort_and_language_are_always_present(self):
        params = build_discover_params(_config())
        assert params['sort_by'] == 'popularity.desc'
        assert params['language'] == 'en-US'

    def test_page_is_clamped_to_bounds(self):
        assert clamp_page(0) == 1
        assert clamp_page(-5) == 1
        assert clamp_page(10_000) == 500
        assert clamp_page('nonsense') == 1
        assert clamp_page(None) == 1
        assert build_discover_params(_config(), page=9999)['page'] == 500

    def test_no_availability_or_provider_params_are_emitted(self):
        params = build_discover_params(
            _config(genres=['action'], min_rating=5, year_from=2000))
        joined = ' '.join(params).lower()
        for forbidden in ('provider', 'watch', 'monetization', 'region'):
            assert forbidden not in joined


# ── URL round trip ─────────────────────────────────────────────────────────

class TestUrlRoundTrip:

    def test_active_filters_are_reflected_in_the_url(self):
        config, notices = parse_discovery_args({
            'type': 'tv', 'genres': 'sci_fi_fantasy,drama',
            'year_from': '2010', 'year_to': '2019',
            'min_rating': '7', 'sort': 'first_air_date.desc'})
        assert notices == []
        assert config['media_type'] == 'tv'
        assert config['genres'] == ['sci_fi_fantasy', 'drama']

    def test_query_round_trips_through_config(self):
        config = _config(media_type='movie', genres=['horror'],
                         year_from=1980, year_to=1990, min_rating=7.5,
                         sort='vote_average.desc')
        again, _ = parse_discovery_args(config_to_query(config))
        assert again == config

    def test_defaults_stay_out_of_the_url(self):
        assert set(config_to_query(_config())) == {'type'}

    def test_invalid_values_are_dropped_with_a_notice_not_a_crash(self):
        config, notices = parse_discovery_args({
            'type': 'anime', 'genres': 'drama,not_a_genre',
            'year_from': 'abcd', 'min_rating': '99', 'sort': 'weird.sort'})
        assert config['media_type'] == 'movie'
        assert config['genres'] == ['drama']
        assert 'year_from' not in config
        assert 'min_rating' not in config
        assert config['sort'] == 'popularity.desc'
        assert len(notices) == 5

    def test_reversed_range_in_url_is_normalised(self):
        config, notices = parse_discovery_args(
            {'year_from': '2010', 'year_to': '2000'})
        assert (config['year_from'], config['year_to']) == (2000, 2010)
        assert any('Swapped' in n for n in notices)

    def test_chips_describe_active_facets(self):
        labels = [c['label'] for c in summarize(
            _config(genres=['horror'], year_from=2000, year_to=2000,
                    min_rating=8))]
        assert 'Movies' in labels
        assert 'Horror' in labels
        assert '2000' in labels
        assert 'Rating ≥ 8' in labels


# ── The /explore page ──────────────────────────────────────────────────────

class TestExplorePage:

    def test_page_renders_without_filters(self, client):
        r = client.get('/explore')
        assert r.status_code == 200
        assert b'Discover' in r.data
        assert b'facet-form' in r.data

    def test_page_reflects_filters_in_markup(self, client):
        r = client.get('/explore?type=tv&genres=drama&year_from=2010')
        assert r.status_code == 200
        assert b'drama' in r.data

    def test_page_survives_garbage_query(self, client):
        r = client.get('/explore?type=xx&genres=@@@&year_from=zz')
        assert r.status_code == 200
        assert b'Some filters were adjusted' in r.data

    def test_server_emits_the_exact_proxy_params(self, client):
        r = client.get('/explore?genres=horror&year_from=1990&min_rating=6')
        assert r.status_code == 200
        body = r.data.decode()
        assert 'primary_release_date.gte' in body
        assert '/discover/movie' in body
        assert 'vote_average.gte' in body

    def test_server_emits_first_air_date_for_tv(self, client):
        r = client.get('/explore?type=tv&year_from=2015')
        assert b'first_air_date.gte' in r.data
        assert b'primary_release_date.gte' not in r.data

    def test_reset_returns_to_an_unfiltered_page(self, client):
        r = client.get('/explore/reset')
        assert r.status_code == 302
        assert r.headers['Location'].endswith('/explore')

    def test_page_is_public(self, client):
        assert client.get('/explore').status_code == 200

    def test_explore_does_not_shadow_the_existing_discover_route(self, client):
        # /discover already belongs to the curated-lists surfaces.
        r = client.get('/discover')
        assert r.status_code in (200, 302)
        assert b'facet-form' not in r.data


# ── Proxy contract ─────────────────────────────────────────────────────────

class TestProxyContract:

    def test_proxy_serves_a_validated_discover_request(self, client, tmdb):
        tmdb.register('/3/discover/movie', {
            'page': 1, 'total_results': 1,
            'results': [{'id': 1, 'title': 'A', 'release_date': '1999-01-01'}],
            'total_pages': 1})
        r = client.get('/api/tmdb/proxy?path=/discover/movie&sort_by=popularity.desc')
        assert r.status_code == 200
        assert r.json['results'][0]['title'] == 'A'

    def test_proxy_still_rejects_paths_outside_the_allowlist(self, client):
        assert client.get(
            '/api/tmdb/proxy?path=/user/account').status_code == 400

    def test_proxy_still_rejects_malformed_paths(self, client):
        assert client.get('/api/tmdb/proxy?path=../../etc').status_code == 400

    def test_discover_needs_no_tmdb_call_to_render(self, client, tmdb):
        client.get('/explore?genres=horror')
        assert tmdb.count('/3/discover') == 0


# ── Preset storage ─────────────────────────────────────────────────────────

class TestPresetStorage:

    def test_kind_discovery_validates_as_a_filter_pair(self):
        filters = validate_config(
            'all', {'kind': DISCOVERY_KIND,
                    'discovery': {'media_type': 'movie'}}, 'date_added')
        assert filters['kind'] == DISCOVERY_KIND

    def test_kind_without_discovery_is_rejected(self):
        with pytest.raises(ValueError):
            validate_config('all', {'kind': DISCOVERY_KIND}, 'date_added')

    def test_discovery_without_kind_is_rejected(self):
        with pytest.raises(ValueError):
            validate_config('all', {'discovery': {'media_type': 'movie'}},
                            'date_added')

    def test_unknown_kind_value_is_rejected(self):
        with pytest.raises(ValueError):
            validate_config('all', {'kind': 'something_else',
                                    'discovery': {'media_type': 'movie'}},
                            'date_added')

    def test_invalid_discovery_config_is_rejected_on_save(self):
        with pytest.raises(ValueError):
            validate_config('all', {'kind': DISCOVERY_KIND,
                                    'discovery': {'media_type': 'anime'}},
                            'date_added')

    def test_ordinary_filters_cannot_be_smuggled_into_a_preset(self):
        with pytest.raises(ValueError):
            validate_config('all', {'kind': DISCOVERY_KIND,
                                    'discovery': {'media_type': 'movie'},
                                    'min_rating': 9}, 'date_added')

    def test_normal_smart_list_shape_is_unchanged(self):
        filters = validate_config('watchlist', {'min_rating': 6}, 'rating')
        assert filters == {'min_rating': 6}

    def test_filters_property_round_trips_a_preset(self, db):
        p = _make_preset(_user(db, 'preset-roundtrip'))
        assert p.filters['discovery']['media_type'] == 'movie'
        assert is_discovery_preset(p)


# ── Preset CRUD API ────────────────────────────────────────────────────────

class TestPresetCrud:

    def test_saving_requires_authentication(self, client):
        r = client.post('/explore/presets', json={'name': 'x', 'config': {}})
        assert r.status_code in (302, 401)

    def test_owner_can_save_a_preset(self, auth_client):
        r = auth_client.post('/explore/presets', json={
            'name': '90s horror', 'config': {
                'media_type': 'movie', 'genres': ['horror'],
                'year_from': 1990, 'year_to': 1999, 'min_rating': 7}})
        assert r.status_code == 201
        preset = r.json['preset']
        assert preset['is_discovery'] is True
        assert preset['query']['genres'] == 'horror'
        assert preset['query']['type'] == 'movie'
        assert preset['query']['year_from'] == 1990

    def test_saving_without_a_name_is_rejected(self, auth_client):
        r = auth_client.post('/explore/presets',
                             json={'name': '   ', 'config': {}})
        assert r.status_code == 400

    def test_saving_an_invalid_config_is_rejected(self, auth_client):
        r = auth_client.post('/explore/presets', json={
            'name': 'bad', 'config': {'media_type': 'anime'}})
        assert r.status_code == 400

    def test_saving_a_malformed_json_body_is_rejected(self, auth_client):
        r = auth_client.post('/explore/presets', json={
            'name': 'bad', 'config': 'not-json'})
        assert r.status_code == 400

    def test_owner_can_delete_their_preset(self, auth_client, sample_user):
        p = _make_preset(sample_user)
        r = auth_client.delete(f'/explore/presets/{p.id}')
        assert r.status_code == 200
        assert SmartList.query.get(p.id) is None

    def test_one_user_cannot_delete_another_users_preset(
            self, auth_client, db, sample_user):
        theirs = _make_preset(_user(db, 'preset-other'))
        r = auth_client.delete(f'/explore/presets/{theirs.id}')
        assert r.status_code == 404
        assert SmartList.query.get(theirs.id) is not None

    def test_presets_appear_in_the_sidebar_for_their_owner(
            self, client, auth_client):
        auth_client.post('/explore/presets', json={
            'name': 'Mine', 'config': {'media_type': 'movie'}})
        assert 'Mine' in client.get('/explore').data.decode()


# ── Restore reopens Explore, never local media ─────────────────────────────

class TestPresetRestore:

    def test_restore_redirects_to_explore_with_the_stored_filters(
            self, auth_client, db, sample_user):
        auth_client.post('/explore/presets', json={
            'name': 'TV 2010s', 'config': {
                'media_type': 'tv', 'genres': ['drama'],
                'year_from': 2010, 'year_to': 2019,
                'sort': 'first_air_date.desc'}})
        preset = (SmartList.query
                  .filter_by(user_id=sample_user.id)
                  .order_by(SmartList.id.desc()).first())
        r = auth_client.get(f'/explore/presets/{preset.id}/restore')
        assert r.status_code == 302
        loc = r.headers['Location']
        assert loc.startswith('/explore?')
        assert 'type=tv' in loc
        assert 'genres=drama' in loc
        assert 'year_from=2010' in loc

    def test_restore_requires_authentication(self, client, db):
        p = _make_preset(_user(db, 'restore-anon'))
        r = client.get(f'/explore/presets/{p.id}/restore')
        assert r.status_code in (302, 401)

    def test_restore_of_another_users_preset_is_404(
            self, auth_client, db, sample_user):
        theirs = _make_preset(_user(db, 'restore-not-mine'))
        assert auth_client.get(
            f'/explore/presets/{theirs.id}/restore').status_code == 404

    def test_restore_of_a_normal_smart_list_is_404(self, auth_client, sample_user):
        normal = _make_normal_list(sample_user)
        r = auth_client.get(f'/explore/presets/{normal.id}/restore')
        assert r.status_code == 404

    def test_restored_page_reflects_the_preset(
            self, auth_client, db, sample_user):
        auth_client.post('/explore/presets', json={
            'name': 'Horror', 'config': {
                'media_type': 'movie', 'genres': ['horror']}})
        preset = (SmartList.query
                  .filter_by(user_id=sample_user.id)
                  .order_by(SmartList.id.desc()).first())
        r = auth_client.get(f'/explore/presets/{preset.id}/restore',
                            follow_redirects=True)
        assert r.status_code == 200
        assert b'with_genres' in r.data

    def test_outdated_config_degrades_safely(self, auth_client, db, sample_user):
        p = _make_preset(sample_user, name='Legacy')
        p.filters_json = (
            '{"kind": "discovery", "discovery": {"media_type": "anime"}}')
        db.session.commit()
        r = auth_client.get(f'/explore/presets/{p.id}/restore')
        assert r.status_code == 400

    def test_corrupt_json_does_not_crash_restore(self, auth_client, db, sample_user):
        p = _make_preset(sample_user, name='Corrupt')
        p.filters_json = '{not json'
        db.session.commit()
        r = auth_client.get(f'/explore/presets/{p.id}/restore')
        assert r.status_code == 404


# ── Isolation from the SQL-first evaluator ─────────────────────────────────

class TestPresetIsolation:

    def test_results_endpoint_refuses_a_preset(self, auth_client, sample_user):
        p = _make_preset(sample_user)
        r = auth_client.get(f'/api/smart-lists/{p.id}/results')
        assert r.status_code == 409
        assert r.json['is_discovery'] is True

    def test_results_endpoint_still_serves_normal_lists(
            self, auth_client, sample_user):
        normal = _make_normal_list(sample_user)
        r = auth_client.get(f'/api/smart-lists/{normal.id}/results')
        assert r.status_code == 200

    def test_evaluate_smart_list_refuses_a_preset(self, db):
        p = _make_preset(_user(db, 'eval-guard'))
        with pytest.raises(ValueError):
            evaluate_smart_list(p)

    def test_preset_detail_page_redirects_to_explore(
            self, auth_client, sample_user):
        p = _make_preset(sample_user)
        r = auth_client.get(f'/smart-lists/{p.id}')
        assert r.status_code == 302
        assert '/explore' in r.headers['Location']

    def test_list_api_marks_presets_distinctly(self, auth_client, sample_user):
        _make_preset(sample_user, name='Preset one')
        _make_normal_list(sample_user, name='Normal one')
        body = auth_client.get('/api/smart-lists').json['smart_lists']
        by_name = {b['name']: b['is_discovery'] for b in body}
        assert by_name['Preset one'] is True
        assert by_name['Normal one'] is False

    def test_preset_is_not_labelled_as_everything(self, db):
        labels = rule_summary(_make_preset(_user(db, 'label-guard')))
        assert 'Everything' not in labels
        assert labels[0] == 'Saved Discovery'

    def test_normal_list_keeps_its_scope_label(self, db):
        normal = _make_normal_list(_user(db, 'label-normal'))
        assert rule_summary(normal)[0] == 'My Watchlist'

    def test_normal_list_evaluation_is_unaffected(self, db):
        normal = _make_normal_list(_user(db, 'eval-normal'))
        result = evaluate_smart_list(normal)
        assert set(result) == {'items', 'total', 'page', 'pages'}

    def test_my_lists_page_shows_presets(self, auth_client, sample_user):
        _make_preset(sample_user, name='Discover preset')
        assert 'Discover preset' in auth_client.get('/lists').data.decode()

    def test_no_new_persistence_model_was_introduced(self, db):
        tables = set(db.metadata.tables)
        assert 'smart_list' in tables
        assert not any('saved_search' in t for t in tables)
        assert not any('discovery' in t for t in tables)
        assert not any('preset' in t for t in tables)


# ── Cross-user isolation ───────────────────────────────────────────────────

class TestOwnership:

    def test_another_users_preset_is_404_on_every_surface(
            self, auth_client, db, sample_user):
        theirs = _make_preset(_user(db, 'not-my-preset'))
        assert auth_client.get(f'/smart-lists/{theirs.id}').status_code == 404
        assert auth_client.get(f'/api/smart-lists/{theirs.id}').status_code == 404
        assert auth_client.get(
            f'/api/smart-lists/{theirs.id}/results').status_code == 404
        assert auth_client.delete(
            f'/explore/presets/{theirs.id}').status_code == 404

    def test_another_users_preset_never_appears_in_my_lists(
            self, auth_client, db, sample_user):
        _make_preset(_user(db, 'invisible-preset'), name='ShouldNotAppear')
        body = auth_client.get('/api/smart-lists').json['smart_lists']
        assert 'ShouldNotAppear' not in [b['name'] for b in body]


# ── Bounded / no-N+1 ───────────────────────────────────────────────────────

class TestBoundedWork:

    def test_discovery_page_rendering_makes_no_sql(self, db):
        from sqlalchemy import event
        from models.base import db as _db
        _make_preset(_user(db, 'no-sql'))
        counts = {'n': 0}

        def _count(*a, **k):
            counts['n'] += 1

        event.listen(_db.engine, 'before_cursor_execute', _count)
        try:
            from app import app as flask_app
            flask_app.test_client().get('/explore?genres=horror&year_from=1990')
        finally:
            event.remove(_db.engine, 'before_cursor_execute', _count)
        assert counts['n'] == 0

    def test_page_is_not_huge(self, client):
        assert len(client.get('/explore').data) < 200_000


# ── Startup safety ─────────────────────────────────────────────────────────

class TestStartupSafety:

    def test_f11_introduces_no_schema_objects(self, db):
        names = set(db.metadata.tables)
        assert 'smart_list' in names
        assert not any('discovery' in n for n in names)
        assert not any('preset' in n for n in names)

# ── Generic PUT must not change a row's type ───────────────────────────────


class TestTypePreservedThroughGenericUpdate:
    """A discovery preset must stay a preset through the generic endpoint.

    The generic Smart List API owns ordinary lists. Letting it flip a row's
    type would silently change what the record means: a preset that became a
    plain list would start being evaluated against the user's local media.
    """

    def test_downgrading_a_preset_to_a_normal_list_is_rejected(
            self, auth_client, db, sample_user):
        preset = _make_preset(sample_user, name='Keep me')
        original = preset.filters_json
        r = auth_client.put(f'/api/smart-lists/{preset.id}', json={
            'filters': {'min_rating': 6}, 'scope': 'watchlist'})
        assert r.status_code == 400
        assert 'Discover' in r.json['error']
        assert SmartList.query.get(preset.id).filters_json == original

    def test_scope_only_change_does_not_convert_a_preset(
            self, auth_client, db, sample_user):
        # `scope` alone does not replace the discriminator, so this edit is
        # allowed — but the row must STILL be a discovery preset afterwards.
        preset = _make_preset(sample_user, name='Scope nudge')
        original = preset.filters_json
        auth_client.put(f'/api/smart-lists/{preset.id}',
                        json={'scope': 'watchlist'})
        after = SmartList.query.get(preset.id)
        assert after.filters_json == original
        assert is_discovery_preset(after)

    def test_preset_survives_a_rejected_request_intact(
            self, auth_client, db, sample_user):
        preset = _make_preset(
            sample_user, name='Intact',
            config=_config(genres=['horror'], year_from=1990, min_rating=7))
        before = SmartList.query.get(preset.id)
        stored = before.filters
        assert is_discovery_preset(before)

        auth_client.put(f'/api/smart-lists/{preset.id}', json={
            'filters': {'min_rating': 2}, 'scope': 'diary', 'sort': 'random'})

        after = SmartList.query.get(preset.id)
        assert is_discovery_preset(after)
        assert after.filters == stored
        assert after.filters['kind'] == DISCOVERY_KIND
        assert after.filters['discovery']['genres'] == ['horror']
        assert after.filters['discovery']['year_from'] == 1990
        assert after.filters['discovery']['min_rating'] == 7

    def test_renaming_a_preset_is_allowed_and_keeps_it_a_preset(
            self, auth_client, db, sample_user):
        preset = _make_preset(sample_user, name='Old name')
        stored = preset.filters
        r = auth_client.put(f'/api/smart-lists/{preset.id}',
                            json={'name': 'New name'})
        assert r.status_code == 200
        assert r.json['name'] == 'New name'
        assert r.json['is_discovery'] is True
        after = SmartList.query.get(preset.id)
        assert after.name == 'New name'
        assert after.filters == stored

    def test_description_update_on_a_preset_is_allowed(
            self, auth_client, db, sample_user):
        preset = _make_preset(sample_user, name='Described')
        r = auth_client.put(f'/api/smart-lists/{preset.id}',
                            json={'description': 'My sci-fi picks'})
        assert r.status_code == 200
        assert r.json['is_discovery'] is True

    def test_idempotent_full_put_of_a_preset_is_allowed(
            self, auth_client, db, sample_user):
        # Re-sending the identical config must not be treated as a downgrade.
        preset = _make_preset(sample_user, name='Idempotent')
        r = auth_client.put(f'/api/smart-lists/{preset.id}', json={
            'name': 'Idempotent', 'filters': preset.filters})
        assert r.status_code == 200
        assert r.json['is_discovery'] is True

    def test_a_normal_list_cannot_be_turned_into_a_preset(
            self, auth_client, db, sample_user):
        normal = _make_normal_list(sample_user, name='Ordinary')
        original = normal.filters_json
        r = auth_client.put(f'/api/smart-lists/{normal.id}', json={
            'filters': {'kind': DISCOVERY_KIND,
                        'discovery': {'media_type': 'movie'}}})
        assert r.status_code == 400
        assert not is_discovery_preset(SmartList.query.get(normal.id))
        assert SmartList.query.get(normal.id).filters_json == original

    def test_normal_smart_list_update_behaviour_is_unchanged(
            self, auth_client, db, sample_user):
        normal = _make_normal_list(sample_user, name='Before')
        r = auth_client.put(f'/api/smart-lists/{normal.id}', json={
            'name': 'After', 'filters': {'min_rating': 8},
            'scope': 'watchlist', 'sort': 'rating'})
        assert r.status_code == 200
        assert r.json['is_discovery'] is False
        assert r.json['filters'] == {'min_rating': 8}
        assert r.json['scope'] == 'watchlist'
        assert r.json['sort'] == 'rating'

    def test_normal_list_partial_update_still_merges(
            self, auth_client, db, sample_user):
        normal = _make_normal_list(sample_user, name='Merged')
        r = auth_client.put(f'/api/smart-lists/{normal.id}',
                            json={'name': 'Merged 2'})
        assert r.status_code == 200
        assert r.json['filters'] == {'min_rating': 6}

    def test_invalid_config_on_a_preset_is_still_rejected(
            self, auth_client, db, sample_user):
        preset = _make_preset(sample_user, name='Bad edit')
        r = auth_client.put(f'/api/smart-lists/{preset.id}', json={
            'filters': {'kind': DISCOVERY_KIND,
                        'discovery': {'media_type': 'anime'}}})
        assert r.status_code == 400
        assert is_discovery_preset(SmartList.query.get(preset.id))

    def test_ownership_on_generic_update_is_unchanged(
            self, auth_client, db, sample_user):
        theirs = _make_preset(_user(db, 'update-not-mine'), name='Theirs')
        r = auth_client.put(f'/api/smart-lists/{theirs.id}',
                            json={'name': 'Hijacked'})
        assert r.status_code == 404
        assert SmartList.query.get(theirs.id).name == 'Theirs'

    def test_dedicated_preset_save_and_restore_still_work(
            self, auth_client, db, sample_user):
        created = auth_client.post('/explore/presets', json={
            'name': 'Owned', 'config': {'media_type': 'tv',
                                        'genres': ['drama']}})
        assert created.status_code == 201
        preset_id = created.json['preset']['id']
        row = SmartList.query.get(preset_id)
        assert is_discovery_preset(row)
        r = auth_client.get(f'/explore/presets/{preset_id}/restore')
        assert r.status_code == 302
        assert 'type=tv' in r.headers['Location']
