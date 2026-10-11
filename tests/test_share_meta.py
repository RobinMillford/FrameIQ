"""F13 — Open Graph / Twitter card / JSON-LD metadata and share control.

Covers:
- the pure helper contract (image selection, fallback, description clamping)
- server-rendered metadata on the public movie/TV/actor detail pages
- privacy: authenticated pages stay noindex and leak nothing
- Year-in-Review share metadata, built only from the already-public payload
- JSON-LD safety: valid JSON, no fabricated ratings, no breakout
"""
import json
import re
from unittest.mock import patch

import pytest

from api import share_meta

# ── helpers ─────────────────────────────────────────────────────────────────


def _og(html, prop):
    m = re.search(r'<meta property="%s" content="([^"]*)"' % re.escape(prop), html)
    return m.group(1) if m else None


def _name(html, name):
    m = re.search(r'<meta name="%s" content="([^"]*)"' % re.escape(name), html)
    return m.group(1) if m else None


def _jsonlds(html):
    return [json.loads(b) for b in re.findall(
        r'<script type="application/ld\+json">(.*?)</script>', html, re.S)]


def _canonical(html):
    m = re.search(r'<link rel="canonical" href="([^"]*)"', html)
    return m.group(1) if m else None


# ── pure helper contract ────────────────────────────────────────────────────

class TestShareMetaHelpers:

    @pytest.mark.parametrize('bad', [None, '', '   ', 'not-a-url',
                                     '//image.tmdb.org/x.jpg',
                                     '/relative.jpg',
                                     'http://insecure.tmdb.org/x.jpg',
                                     'javascript:alert(1)'])
    def test_untrusted_image_urls_are_rejected(self, bad):
        assert not share_meta.usable_image_url(bad)

    def test_third_party_placeholder_is_rejected(self):
        assert not share_meta.usable_image_url(
            'https://via.placeholder.com/500x750?text=No+Image')

    def test_real_tmdb_image_is_accepted(self):
        assert share_meta.usable_image_url(
            'https://image.tmdb.org/t/p/w500/a.jpg')

    def test_backdrop_wins_over_poster(self):
        assert share_meta.select_image(
            'https://image.tmdb.org/t/p/original/b.jpg',
            'https://image.tmdb.org/t/p/w500/p.jpg', 'FB') == \
            'https://image.tmdb.org/t/p/original/b.jpg'

    def test_poster_used_when_backdrop_missing_or_placeholder(self):
        poster = 'https://image.tmdb.org/t/p/w500/p.jpg'
        assert share_meta.select_image(None, poster, 'FB') == poster
        assert share_meta.select_image(
            'https://via.placeholder.com/1920x1080', poster, 'FB') == poster

    def test_local_fallback_used_when_nothing_usable(self):
        assert share_meta.select_image(None, None, 'FB') == 'FB'
        assert share_meta.select_image('x', 'y', 'FB') == 'FB'

    def test_description_falls_back_when_overview_missing(self):
        assert share_meta.build_description(None, 'FB') == 'FB'
        assert share_meta.build_description('   ', 'FB') == 'FB'

    def test_description_is_clamped_on_a_word_boundary(self):
        long = ' '.join(['word%d' % i for i in range(200)])
        out = share_meta.build_description(long, 'FB', limit=50)
        assert len(out) <= 51
        assert out.endswith('…')
        assert 'word49' not in out  # not cut mid-token

    def test_clean_text_collapses_whitespace(self):
        assert share_meta.clean_text('a\n\n  b\tc') == 'a b c'

    def test_jsonld_never_invents_a_rating(self):
        data = share_meta.movie_jsonld('M', 'd', 'i', 'u')
        assert 'aggregateRating' not in data
        assert data['@type'] == 'Movie'

    def test_jsonld_omits_absent_fields(self):
        data = share_meta.tv_series_jsonld('S', '', None, 'u')
        assert 'description' not in data
        assert 'image' not in data
        assert 'startDate' not in data

    def test_webpage_jsonld_carries_no_author_or_rating(self):
        data = share_meta.webpage_jsonld('n', 'd', 'u', 'i')
        assert 'author' not in data
        assert 'aggregateRating' not in data
        assert data['@type'] == 'WebPage'


# ── movie detail ────────────────────────────────────────────────────────────

MOVIE = {
    'id': 27205, 'title': 'Inception',
    'overview': 'A thief who steals corporate secrets through dream-sharing '
                'technology.',
    'tagline': 'Your mind is the scene of the crime.',
    'release_date': '2010-07-15',
    'poster_path': 'https://image.tmdb.org/t/p/w500/p.jpg',
    'backdrop_path': 'https://image.tmdb.org/t/p/original/b.jpg',
    'genres': ['Action', 'Science Fiction'],
    'cast': [{'id': 1, 'name': 'Leonardo DiCaprio'}],
    'director': 'Christopher Nolan',
    'vote_average': 8.4, 'vote_count': 1000,
    # Keys the detail template reads; absent ones make it fall back.
    'runtime': 148, 'trailer_url': None, 'crew': [], 'reviews': [],
    'status': 'Released', 'budget': 0, 'revenue': 0,
    'writer': '', 'original_language': 'en',
}


@pytest.fixture
def movie_page(client):
    with patch('routes.details.fetch_movie_details', return_value=MOVIE):
        return client.get('/movie/27205').data.decode()


class TestMovieMetadata:

    def test_title_and_type(self, movie_page):
        assert '<title>Inception - FrameIQ</title>' in movie_page
        assert _og(movie_page, 'og:type') == 'video.movie'

    def test_description_comes_from_the_overview(self, movie_page):
        assert 'dream-sharing' in (_name(movie_page, 'description') or '')
        assert 'dream-sharing' in (_og(movie_page, 'og:description') or '')

    def test_canonical_and_og_url_are_absolute_and_identical(self, movie_page):
        canonical = _canonical(movie_page)
        assert canonical.startswith('http://localhost/movie/27205')
        assert _og(movie_page, 'og:url') == canonical

    def test_indexable(self, movie_page):
        assert _name(movie_page, 'robots') == 'index, follow'

    def test_twitter_card_present(self, movie_page):
        assert _name(movie_page, 'twitter:card') == 'summary_large_image'
        assert _name(movie_page, 'twitter:title')

    def test_image_is_absolute_https(self, movie_page):
        image = _og(movie_page, 'og:image')
        assert image.startswith('https://')
        assert 'placeholder.com' not in image

    def test_image_alt_describes_the_title(self, movie_page):
        assert 'Inception' in (_og(movie_page, 'og:image:alt') or '')

    def test_no_unverified_dimensions_are_claimed(self, movie_page):
        for field in ('og:image:width', 'og:image:height', 'og:image:type'):
            assert _og(movie_page, field) is None

    def test_jsonld_is_movie_and_valid(self, movie_page):
        blocks = _jsonlds(movie_page)
        assert blocks
        movie = [b for b in blocks if b['@type'] == 'Movie']
        assert movie
        assert movie[0]['name'] == 'Inception'
        assert movie[0]['url'].endswith('/movie/27205')
        assert 'aggregateRating' not in movie[0]

    def test_no_duplicate_canonical_or_og_tags(self, movie_page):
        assert movie_page.count('rel="canonical"') == 1
        for prop in ('og:title', 'og:type', 'og:url', 'og:image'):
            assert movie_page.count('property="%s"' % prop) == 1


# ── TV detail ───────────────────────────────────────────────────────────────

SHOW = {
    'id': 1399, 'name': 'Game of Thrones',
    'overview': 'Nine noble families fight for control of the lands.',
    'first_air_date': '2011-04-17',
    'poster_path': 'https://image.tmdb.org/t/p/w500/p.jpg',
    'backdrop_path': 'https://image.tmdb.org/t/p/original/b.jpg',
    'genres': ['Drama'], 'cast': [{'id': 2, 'name': 'Emilia Clarke'}],
    'creator': 'David Benioff', 'vote_average': 8.4, 'vote_count': 500,
    'runtime': 60, 'trailer_url': None, 'crew': [], 'reviews': [],
    'status': 'Ended', 'budget': 0, 'revenue': 0, 'writer': '',
    'original_language': 'en', 'networks': [], 'seasons': [],
    'number_of_seasons': 8, 'number_of_episodes': 73,
    'episode_run_time': [60], 'created_by': [], 'certification': 'TV-MA',
    'last_air_date': '2019-05-19', 'origin_country': ['US'],
    'tagline': 'Winter is coming.', 'trailer_key': None,
}


@pytest.fixture
def tv_page(client):
    with patch('routes.details.fetch_tv_show_details', return_value=SHOW):
        return client.get('/tv/1399').data.decode()


class TestTvMetadata:

    def test_uses_tv_context_and_url(self, tv_page):
        assert '<title>Game of Thrones - FrameIQ</title>' in tv_page
        assert _canonical(tv_page).endswith('/tv/1399')
        assert _og(tv_page, 'og:type') == 'video.tv_show'

    def test_jsonld_is_tvseries_with_startdate(self, tv_page):
        blocks = [b for b in _jsonlds(tv_page) if b['@type'] == 'TVSeries']
        assert blocks
        assert blocks[0]['startDate'] == '2011-04-17'
        assert 'aggregateRating' not in blocks[0]

    def test_description_from_tv_overview(self, tv_page):
        assert 'Nine noble families' in (_name(tv_page, 'description') or '')

    def test_image_present_and_trusted(self, tv_page):
        assert _og(tv_page, 'og:image').startswith('https://image.tmdb.org')


# ── privacy ─────────────────────────────────────────────────────────────────

class TestPrivacy:

    def test_diary_is_noindex(self, client, auth_client):
        html = auth_client.get('/diary').data.decode()
        assert _name(html, 'robots') == 'noindex, nofollow'
        assert _og(html, 'og:description') is None
        assert _og(html, 'og:title') is None

    def test_profile_emits_no_user_data_in_metadata(self, client, auth_client):
        html = auth_client.get('/profile').data.decode()
        assert _name(html, 'robots') == 'noindex, nofollow'
        assert _og(html, 'og:title') is None
        assert _og(html, 'og:description') is None
        assert _jsonlds(html) == []
        assert 'testuser' not in (_name(html, 'description') or '')

    def test_user_profile_leaks_nothing(self, client, auth_client, db):
        from models import User
        with client.application.app_context():
            other = User(username='privateperson', email='pp@example.com')
            other.set_password('Pw12345678')
            db.session.add(other)
            db.session.commit()
            other_id = other.id
        html = auth_client.get(f'/user/{other_id}').data.decode()
        assert _name(html, 'robots') == 'noindex, nofollow'
        # The <title> naming the profile owner is pre-existing and correct for
        # a page the viewer may already see. What must NOT happen is the owner
        # (or any profile data) reaching PREVIEW metadata a crawler reads.
        for value in (_og(html, 'og:title'), _og(html, 'og:description'),
                      _name(html, 'twitter:title'),
                      _name(html, 'twitter:description'),
                      _name(html, 'description'), _canonical(html)):
            assert value is None or 'privateperson' not in value
        assert _jsonlds(html) == []

    def test_base_template_pages_default_to_noindex(self, client):
        html = client.get('/').data.decode()
        assert _name(html, 'robots') == 'noindex, nofollow'

    def test_unauthenticated_profile_gets_no_metadata(self, client):
        r = client.get('/user/1')
        assert r.status_code in (301, 302, 401)
        assert b'og:title' not in r.data or r.status_code == 200


# ── injection safety ────────────────────────────────────────────────────────

class TestInjectionSafety:

    def test_hostile_tmdb_text_cannot_break_out_of_attributes(self, client):
        hostile = dict(
            MOVIE, id=99, title='"><script>alert(1)</script>',
            overview='</script><script>alert(2)</script>')
        with patch('routes.details.fetch_movie_details', return_value=hostile):
            html = client.get('/movie/99').data.decode()
        head = html.split('</head>')[0]
        assert '<script>alert(1)</script>' not in head
        assert '<script>alert(2)</script>' not in head

    def test_hostile_text_still_produces_valid_jsonld(self, client):
        nasty = dict(
            MOVIE, id=98, title='</script><script>x</script>',
            overview='quoted " line and a backslash')
        with patch('routes.details.fetch_movie_details', return_value=nasty):
            html = client.get('/movie/98').data.decode()
        blocks = _jsonlds(html)
        assert blocks
        assert any(b.get('name', '').startswith('</script>') for b in blocks)


# ── Year in Review public share ─────────────────────────────────────────────


def _seed_share(app, db, year=2026, entries=3):
    """A real, shareable recap: the route 404s on an empty history, so the
    preview tests must seed canonical diary rows rather than skip."""
    import uuid
    import api.year_in_review_share as svc
    from models import User, MediaItem, DiaryEntry, YearInReviewShare
    with app.app_context():
        u = User(username='sharer_%d' % (uuid.uuid4().int % 90000),
                 email='sharer%d@example.com' % (uuid.uuid4().int % 90000))
        u.set_password('TestPass1')
        db.session.add(u)
        db.session.commit()
        for i in range(entries):
            m = MediaItem(tmdb_id=9_800_000 + (uuid.uuid4().int % 900_000),
                          media_type='movie', title='Film %d' % i,
                          runtime=100 + i, genres='Drama')
            db.session.add(m)
            db.session.commit()
            db.session.add(DiaryEntry(
                user_id=u.id, media_id=m.id, media_type='movie',
                watched_date=__import__('datetime').date(year, 3, 1 + i),
                rating=4.5, is_rewatch=False))
        db.session.commit()
        token = svc.generate_share_token()
        db.session.add(YearInReviewShare(
            user_id=u.id, year=year, token_hash=svc.hash_share_token(token)))
        db.session.commit()
        return token


def test_year_in_review_share_metadata(app, client, db):
    """The share preview uses ONLY the already-public payload."""
    token = _seed_share(app, db)
    r = client.get('/share/year-in-review/%s' % token)
    assert r.status_code == 200
    html = r.data.decode()

    assert _name(html, 'robots') == 'noindex, nofollow'
    assert '2026' in _og(html, 'og:title')
    assert _og(html, 'og:description')
    assert _og(html, 'og:image')
    assert _canonical(html).startswith('http://localhost/share/year-in-review/')
    assert _name(html, 'twitter:card')

    for block in _jsonlds(html):
        assert block['@type'] == 'WebPage'
        assert 'aggregateRating' not in block
        assert 'author' not in block


def test_year_in_review_share_description_is_derived_not_generic(
        app, client, db):
    token = _seed_share(app, db, year=2024)
    r = client.get('/share/year-in-review/%s' % token)
    assert r.status_code == 200
    desc = _og(r.data.decode(), 'og:description')
    assert desc and desc != 'A Year in Review recap shared on FrameIQ.'
    assert '2024' in desc


def test_share_control_uses_the_canonical_url(client):
    with patch('routes.details.fetch_movie_details', return_value=MOVIE):
        html = client.get('/movie/27205').data.decode()
    assert 'id="detail-share"' in html
    assert 'data-share-url="%s"' % _canonical(html) in html
    assert 'data-share-button' in html
    assert 'role="status"' in html
    assert 'share-link.js' in html
