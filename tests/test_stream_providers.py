"""
Tests for stream provider registry — URL building and source ordering.
Verifies: RiveStream default, all provider URL shapes (movie/tv), resume
param appending, unknown provider handling, and watch route integration.
"""
from api.stream_providers import (
    PROVIDERS, ALLOWED_ORIGINS, build_embed_url, get_sources,
)


class TestBuildEmbedUrl:
    def test_rive_movie(self):
        url = build_embed_url('rive', 'movie', 533535)
        assert url == 'https://www.rivestream.app/embed?type=movie&id=533535'

    def test_rive_tv(self):
        url = build_embed_url('rive', 'tv', 1396, season=1, episode=2)
        assert (
            url == 'https://www.rivestream.app/embed'
            '?type=tv&id=1396&season=1&episode=2'
        )

    def test_rive_agg_movie(self):
        url = build_embed_url('rive_agg', 'movie', 278)
        assert url == 'https://www.rivestream.app/embed/agg?type=movie&id=278'

    def test_rive_torrent_tv(self):
        url = build_embed_url('rive_torrent', 'tv', 1399, season=2, episode=7)
        assert (
            url == 'https://www.rivestream.app/embed/torrent'
            '?type=tv&id=1399&season=2&episode=7'
        )

    def test_vidking_movie(self):
        url = build_embed_url('vidking', 'movie', 533535)
        assert url == 'https://www.vidking.net/embed/movie/533535'

    def test_vidking_tv(self):
        url = build_embed_url('vidking', 'tv', 1396, season=1, episode=1)
        assert url == 'https://www.vidking.net/embed/tv/1396/1/1'

    def test_resume_appends_query_for_path_style_urls(self):
        url = build_embed_url(
            'vidking', 'movie', 533535, resume_time=615
        )
        assert url.endswith('?t=615')

    def test_resume_uses_ampersand_when_query_exists(self):
        url = build_embed_url(
            'rive', 'tv', 1396, season=1, episode=1, resume_time=90
        )
        assert url.endswith('&t=90')

    def test_no_resume_param_at_zero(self):
        url = build_embed_url('rive', 'movie', 278, resume_time=0)
        assert 't=' not in url

    def test_unknown_provider_returns_none(self):
        assert build_embed_url('nope', 'movie', 123) is None

    def test_tv_requires_season_and_episode(self):
        assert build_embed_url('rive', 'tv', 1396) is None
        assert build_embed_url('rive', 'tv', 1396, season=1) is None


class TestGetSources:
    def test_default_source_first(self):
        sources = get_sources('movie', 533535)
        assert sources[0]['key'] == 'rive'

    def test_all_providers_present_and_ordered(self):
        sources = get_sources('movie', 533535)
        keys = [s['key'] for s in sources]
        assert set(keys) == set(PROVIDERS.keys())
        # Default first, remaining providers follow without duplicates
        assert len(keys) == len(set(keys))

    def test_every_source_has_label_and_url(self):
        for src in get_sources('tv', 1396, season=1, episode=1):
            assert src['label']
            assert src['url'].startswith('https://')

    def test_allowed_origins_cover_all_bases(self):
        bases = {p['base'] for p in PROVIDERS.values()}
        assert bases == set(ALLOWED_ORIGINS)


class TestWatchRouteIntegration:
    def test_watch_page_includes_source_switcher(self, client):
        r = client.get('/watch/movie/278')
        assert r.status_code == 200
        html = r.get_data(as_text=True)
        assert 'source-switcher' in html
        assert 'rivestream.app' in html
        assert 'Server 1' in html

    def test_provider_override_via_query_param(self, client):
        r = client.get('/watch/movie/278?provider=vidking')
        html = r.get_data(as_text=True)
        # VidKing URL becomes the active iframe source
        assert 'https://www.vidking.net/embed/movie/278' in html

    def test_invalid_provider_falls_back_to_default(self, client):
        r = client.get('/watch/movie/278?provider=hacker')
        assert r.status_code == 200
        html = r.get_data(as_text=True)
        assert '/embed?type=movie&amp;id=278' in html
