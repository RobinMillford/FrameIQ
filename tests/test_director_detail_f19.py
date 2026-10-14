"""Feature F19 — director detail pages and navigable Taste DNA chips.

Two halves, deliberately kept shallow:

1. ``GET /director/<id>`` serves a director and their locally-linked films.
   It is served ENTIRELY from local rows and makes NO TMDb call — the
   Director/MediaDirector relationships are already the canonical local source,
   so reaching out to TMDb would add a dependency and a failure mode to a page
   that needs neither. It also reads no viewer-specific state, so nothing
   private can reach a public page.

2. Taste DNA director chips become links when a displayed name resolves to
   EXACTLY ONE persisted Director. The affinity map is keyed by display name,
   so this is the same guarded resolution F14 used for cast: ambiguous or
   unknown names stay plain text, never a wrong link.

The historical enrichment defect (F16) is unrecovered, so the page never claims
a director has no films — only that FrameIQ has no linked credits for them yet.
"""
import socket
import uuid
from datetime import date

import pytest

from api import taste_profile as tp
from models import MediaItem, db
from models.director import Director, MediaDirector


# ── fixtures ────────────────────────────────────────────────────────────────

_SEQ = {'n': 8_800_000}


def _uniq(prefix):
    return '%s_%s' % (prefix, uuid.uuid4().hex[:8])


def _next_id():
    _SEQ['n'] += 1
    return _SEQ['n']


@pytest.fixture(autouse=True)
def _ctx(app):
    with app.app_context():
        yield


_CREATED = {'directors': [], 'movies': [], 'links': [], 'users': [],
            'profiles': []}


@pytest.fixture(autouse=True)
def _release(app, db):
    """Delete exactly what this module created.

    `app` is session-scoped with no per-test teardown, so director/media rows
    left behind would be picked up by later files that select pending movies
    (test_director_capture does), turning this suite into a cross-file
    ordering hazard. Only rows this module created are removed.
    """
    yield
    from models import TasteProfile, User
    from models.cast import Person
    with app.app_context():
        db.session.rollback()
        # Bulk deletes BY ID: the tracked objects belong to the session that
        # has since been replaced, so deleting the instances directly would
        # fail with "already attached to session".
        for key, model in (('links', MediaDirector),
                           ('profiles', TasteProfile),
                           ('directors', Director),
                           ('movies', MediaItem),
                           ('users', User),
                           ('people', Person)):
            ids = [row.id for row in _CREATED.get(key, []) if row.id]
            if ids:
                db.session.query(model).filter(model.id.in_(ids)).delete(
                    synchronize_session=False)
            _CREATED[key] = []
        db.session.commit()


def _director(name=None):
    d = Director(name=name or _uniq('Director'), tmdb_person_id=_next_id(),
                 source='tmdb')
    db.session.add(d)
    db.session.commit()
    _CREATED['directors'].append(d)
    return d


def _movie(title=None, release_date=None, poster_path=None,
           media_type='movie'):
    m = MediaItem(tmdb_id=_next_id(), media_type=media_type,
                  title=title or _uniq('Film'), runtime=110, genres='Drama',
                  release_date=release_date,
                  poster_path=poster_path)
    db.session.add(m)
    db.session.commit()
    _CREATED['movies'].append(m)
    return m


def _credit(director, movie):
    link = MediaDirector(media_item_id=movie.id, director_id=director.id)
    db.session.add(link)
    db.session.commit()
    _CREATED['links'].append(link)
    return link


def _user():
    from models import User
    username = _uniq('f19user')
    u = User(username=username, email='%s@example.com' % username,
             email_verified=True)
    u.set_password('TestPass1')
    db.session.add(u)
    db.session.commit()
    _CREATED['users'].append(u)
    return u.id, username


def _taste_profile(uid, *, directors=None, titles=12):
    from models import TasteProfile
    p = TasteProfile(user_id=uid)
    p.genre_weights = {'Drama': 0.9}
    p.director_affinity = directors or {}
    p.confidence = 0.7
    p.distinct_title_count = titles
    p.signal_count = 20
    db.session.add(p)
    db.session.commit()
    _CREATED['profiles'].append(p)
    return p


# ════════════════════════════════════════════════════════════════════════════
# Director detail page
# ════════════════════════════════════════════════════════════════════════════

def test_director_page_renders(client):
    d = _director('Christopher Nolan')
    _credit(d, _movie('Inception', date(2010, 7, 16)))
    r = client.get('/director/%d' % d.id)
    assert r.status_code == 200
    body = r.data.decode()
    assert 'Christopher Nolan' in body
    assert 'Inception' in body
    assert '2010' in body


def test_credit_links_to_the_right_movie(client):
    d = _director()
    a = _movie('Alpha Film', date(2001, 5, 1))
    b = _movie('Beta Film', date(1999, 3, 2))
    _credit(d, a)
    _credit(d, b)
    body = client.get('/director/%d' % d.id).data.decode()
    assert '/movie/%d' % a.tmdb_id in body
    assert '/movie/%d' % b.tmdb_id in body


def test_missing_director_returns_404(client):
    r = client.get('/director/999999999')
    assert r.status_code == 404


def test_director_with_no_credits_renders_an_honest_empty_state(client):
    """F16 left many directors with no linked rows. That is NOT 'no films'."""
    d = _director('Uncredited Here')
    body = client.get('/director/%d' % d.id).data.decode()
    assert 'No films are linked to this director in FrameIQ yet' in body
    # It must not claim the director has no work, nor claim completeness.
    assert 'no films' not in body.lower().replace(
        'no films are linked to this director in frameiq yet', '')
    assert 'not the director' in body          # "not the director's career"
    assert 'partial by design' in body


def test_tv_credits_are_excluded_from_a_movie_filmography(client):
    """Only movie credits belong on a director page."""
    d = _director()
    _credit(d, _movie('A Movie', date(2015, 1, 1), media_type='movie'))
    _credit(d, _movie('A Show', date(2016, 1, 1), media_type='tv'))
    body = client.get('/director/%d' % d.id).data.decode()
    assert 'A Movie' in body
    assert 'A Show' not in body


def test_credits_use_deterministic_ordering(client):
    """Newest first, then title, then id — a total order, so paging is stable."""
    d = _director()
    _credit(d, _movie('Zebra', date(2001, 1, 1)))
    _credit(d, _movie('Apple', date(2001, 1, 1)))   # same date -> title order
    _credit(d, _movie('Older', date(1990, 1, 1)))
    _credit(d, _movie('Newer', date(2020, 1, 1)))
    _credit(d, _movie('Undated', None))
    body = client.get('/director/%d' % d.id).data.decode()
    order = [body.index(t) for t in ('Newer', 'Apple', 'Zebra', 'Older',
                                     'Undated')]
    assert order == sorted(order), order


def test_page_makes_no_network_call(client, monkeypatch):
    def _boom(*a, **k):
        raise AssertionError('the director page must not call out')

    monkeypatch.setattr(socket, 'socket', _boom)
    monkeypatch.setattr(socket, 'create_connection', _boom)
    monkeypatch.setattr(
        'api.tmdb.movies.fetch_movie_details',
        lambda *a, **k: _boom())
    d = _director()
    _credit(d, _movie())
    assert client.get('/director/%d' % d.id).status_code == 200


def test_page_reads_no_private_user_state(client):
    """A public page must not render the viewer's watchlist/diary/ratings."""
    uid, username = _user()
    d = _director()
    _credit(d, _movie())
    page = client.get('/director/%d' % d.id).data.decode()
    assert 'watchlist' not in page.lower()
    assert 'diary' not in page.lower()
    logged_in = client.post('/login', data={'username': username,
                                            'password': 'TestPass1'})
    assert logged_in.status_code in (200, 302)
    page2 = client.get('/director/%d' % d.id).data.decode()
    # Still the same public content; no private rows leaked in.
    assert d.name in page2
    assert '/api/taste-profile' not in page2


def test_filmography_is_paginated(app):
    """A large filmography must not render unbounded."""
    from routes.details import DIRECTOR_FILMOGRAPHY_PER_PAGE
    d = _director()
    for i in range(DIRECTOR_FILMOGRAPHY_PER_PAGE + 5):
        _credit(d, _movie('Film %02d' % i,
                          date(2000 + i, 1, 1)))
    body = app.test_client().get('/director/%d' % d.id).data.decode()
    assert 'Page 1 of 2' in body
    assert 'Next' in body
    page2 = app.test_client().get(
        '/director/%d?page=2' % d.id).data.decode()
    assert 'Page 2 of 2' in page2
    # the two pages must show different films
    assert body != page2


def test_page_does_not_invent_metadata(client):
    """No biography, awards, or fabricated imagery."""
    d = _director('Sparse Person')
    _credit(d, _movie())
    body = client.get('/director/%d' % d.id).data.decode()
    assert 'Biography' not in body
    assert 'Awards' not in body
    assert 'Born' not in body


def test_page_declares_public_indexing_metadata(client):
    """Directors are public pages, like /actor."""
    d = _director('Indexed Person')
    _credit(d, _movie())
    head = client.get('/director/%d' % d.id).data.decode().split('</head>')[0]
    assert 'index, follow' in head
    assert '/director/%d' % d.id in head


# ════════════════════════════════════════════════════════════════════════════
# Query safety
# ════════════════════════════════════════════════════════════════════════════

def test_page_issues_a_bounded_number_of_queries(client):
    """Two lookups (director, filmography) regardless of film count."""
    from sqlalchemy import event
    d = _director()
    for i in range(12):
        _credit(d, _movie('Film %02d' % i, date(2000 + i, 1, 1)))
    statements = []

    def _record(_conn, _cur, stmt, *a, **k):
        statements.append(' '.join(stmt.split()).lower())
    event.listen(db.engine, 'before_cursor_execute', _record)
    try:
        client.get('/director/%d' % d.id)
    finally:
        event.remove(db.engine, 'before_cursor_execute', _record)
    media_lookups = [s for s in statements if 'media_director' in s]
    # paginate() issues exactly two statements for this join: one COUNT and one
    # page fetch. Twelve films must cost the same as one — that is the
    # no-N+1 property, and it does not scale with the film count.
    assert len(media_lookups) == 2, media_lookups


# ════════════════════════════════════════════════════════════════════════════
# Link resolution
# ════════════════════════════════════════════════════════════════════════════

def test_resolves_unique_name_to_local_url():
    d = _director('Unique Person')
    assert tp.resolve_director_links(['Unique Person']) == {
        'Unique Person': '/director/%d' % d.id}


def test_unknown_name_resolves_to_nothing():
    assert tp.resolve_director_links([_uniq('Ghost')]) == {}


def test_ambiguous_name_is_never_linked():
    """Two Directors sharing a name must resolve to NEITHER."""
    shared = _uniq('Shared')
    for _ in range(2):
        row = Director(name=shared, tmdb_person_id=_next_id())
        db.session.add(row)
        _CREATED['directors'].append(row)
    db.session.commit()
    assert tp.resolve_director_links([shared]) == {}


def test_rename_is_not_fabricated():
    _director('Current Name')
    assert tp.resolve_director_links([_uniq('Previous')]) == {}


def test_resolve_is_one_batched_query():
    from sqlalchemy import event
    names = [_director().name for _ in range(6)]
    statements = []

    def _record(_conn, _cur, stmt, *a, **k):
        statements.append(stmt)
    event.listen(db.engine, 'before_cursor_execute', _record)
    try:
        links = tp.resolve_director_links(names)
    finally:
        event.remove(db.engine, 'before_cursor_execute', _record)
    assert len(links) == 6
    assert len(statements) == 1


def test_resolve_empty_input_costs_no_query():
    from sqlalchemy import event
    statements = []

    def _record(*a, **k):
        statements.append(1)
    event.listen(db.engine, 'before_cursor_execute', _record)
    try:
        assert tp.resolve_director_links([]) == {}
        assert tp.resolve_director_links(None) == {}
    finally:
        event.remove(db.engine, 'before_cursor_execute', _record)
    assert statements == []


def test_resolve_ignores_foreign_url_schemes():
    entries = tp._director_entries(
        {'A': 0.9},
        {'A': 'https://evil.example/x', 'B': '/director/', 'C': 42})
    for entry in entries:
        assert 'url' not in entry


# ════════════════════════════════════════════════════════════════════════════
# Taste DNA integration
# ════════════════════════════════════════════════════════════════════════════

def test_taste_dna_links_a_uniquely_matched_director(client):
    d = _director('Linked Director')
    uid, username = _user()
    _taste_profile(uid, directors={'Linked Director': 0.9})
    client.post('/login', data={'username': username,
                                'password': 'TestPass1'})
    body = client.get('/api/taste-profile').get_json()
    assert body['top_directors'][0] == {
        'name': 'Linked Director', 'strength': 'high',
        'url': '/director/%d' % d.id}
    assert body['directors_status'] == 'ready'


def test_taste_dna_leaves_ambiguous_director_unlinked(client):
    shared = _uniq('Ambiguous')
    for _ in range(2):
        row = Director(name=shared, tmdb_person_id=_next_id())
        db.session.add(row)
        _CREATED['directors'].append(row)
    db.session.commit()
    uid, username = _user()
    _taste_profile(uid, directors={shared: 0.9})
    client.post('/login', data={'username': username,
                                'password': 'TestPass1'})
    entry = client.get('/api/taste-profile').get_json()['top_directors'][0]
    assert entry['name'] == shared
    assert 'url' not in entry


def test_taste_dna_payload_exposes_no_internal_ids(client):
    d = _director('Identity Director')
    uid, username = _user()
    _taste_profile(uid, directors={'Identity Director': 0.9})
    client.post('/login', data={'username': username,
                                'password': 'TestPass1'})
    body = client.get('/api/taste-profile').get_json()
    text = str(body)
    # Assert the SHAPE rather than matching bare digits: small integer ids
    # appear incidentally inside weights and counts, so a digit-substring
    # check is a coin flip. What matters is that no identity FIELD is exposed
    # and the id occurs only as the route segment.
    for entry in body['top_directors']:
        assert set(entry) == {'name', 'strength', 'url'}
    assert body['top_directors'][0]['url'] == '/director/%d' % d.id
    assert 'tmdb' not in text.lower()
    assert 'tmdb_person_id' not in text
    assert 'user_id' not in text
    assert 'profile_version' not in text
    # The TMDb person id must not leak anywhere, including the URL.
    assert str(d.tmdb_person_id) not in text


def test_f18_three_states_still_hold(client):
    """F19 must not regress F18's honest empty states."""
    uid, username = _user()
    client.post('/login', data={'username': username,
                                'password': 'TestPass1'})
    # ready
    d = _director('Ready Person')
    p = _taste_profile(uid, directors={'Ready Person': 0.9})
    assert client.get('/api/taste-profile').get_json()[
        'directors_status'] == 'ready'
    # no_positive_directors
    p.director_affinity = {'Ready Person': -0.8}
    db.session.commit()
    assert client.get('/api/taste-profile').get_json()[
        'directors_status'] == 'no_positive_directors'
    # no_director_data
    p.director_affinity = {}
    db.session.commit()
    assert client.get('/api/taste-profile').get_json()[
        'directors_status'] == 'no_director_data'
    assert d.id  # sanity


def test_actor_chips_are_untouched(client):
    """F19 changes directors only."""
    from models.cast import Person
    person = Person(name=_uniq('Actor'), tmdb_person_id=_next_id())
    db.session.add(person)
    db.session.commit()
    _CREATED.setdefault('people', []).append(person)
    uid, username = _user()
    p = _taste_profile(uid, directors={'Someone': 0.9})
    p.actor_affinity = {person.name: 0.9}
    db.session.commit()
    client.post('/login', data={'username': username,
                                'password': 'TestPass1'})
    body = client.get('/api/taste-profile').get_json()
    assert [a['name'] for a in body['top_actors']] == [person.name]
    assert body['top_actors'][0]['url'] == '/actor/%d' % person.tmdb_person_id
    assert body['actors_status'] == 'ready'


def test_sort_order_and_limit_are_preserved(client):
    many = {'Dir %02d' % i: 0.5 for i in range(20)}
    uid, username = _user()
    _taste_profile(uid, directors=many)
    client.post('/login', data={'username': username,
                                'password': 'TestPass1'})
    body = client.get('/api/taste-profile').get_json()
    assert len(body['top_directors']) == tp.DNA_TOP_DIRECTORS
    names = [d['name'] for d in body['top_directors']]
    assert names == sorted(names)


def test_cinebot_call_signature_is_unchanged():
    """CineBot calls taste_dna(profile) with no link maps."""
    import inspect
    params = list(inspect.signature(tp.taste_dna).parameters)
    assert params[0] == 'profile'
    assert params[1:] == ['actor_links', 'director_links']
    assert all(p.default is None for p in
               inspect.signature(tp.taste_dna).parameters.values()
               if p.name != 'profile')


# ════════════════════════════════════════════════════════════════════════════
# Browser rendering of linked vs unlinked chips (real markup + real JS)
# ════════════════════════════════════════════════════════════════════════════

# F24: the driver import is REQUIRED in CI and skippable locally.
# See tests/browser_requirement.py for why this replaced a bare
# pytest.importorskip.
from tests.browser_requirement import require_playwright  # noqa: E402

sync_playwright = require_playwright('test_director_detail_f19.py')
playwright_api = sync_playwright


def _render_chromium(taste_browser, payload):
    """Drive the REAL static/js/taste-dna.js against the REAL profile markup."""
    import pathlib
    from playwright.sync_api import sync_playwright  # noqa: F401
    import sys
    root = pathlib.Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(root / 'tests'))
    from test_taste_dna_actor_f14_browser import html_for  # harness builder

    page = taste_browser.new_context().new_page()
    errors = []
    page.on('pageerror', lambda e: errors.append(str(e)))
    page.set_content(html_for(payload))
    page.wait_for_selector('#taste-dna-content:not(.hidden)', timeout=5000)
    page.f19_errors = errors
    return page


@pytest.fixture(scope='module')
def taste_browser():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        b = p.chromium.launch()
        yield b
        b.close()


def _base_payload(**extra):
    return {
        'available': True, 'level': 'strong', 'confidence': 0.7,
        'titles_analyzed': 12,
        'top_genres': [{'name': 'Drama', 'strength': 'high'}],
        'eras': [{'name': '2010s', 'strength': 'high'}],
        **extra,
    }


def test_browser_linked_director_is_an_anchor(taste_browser):
    page = _render_chromium(taste_browser, _base_payload(
        directors_status='ready',
        top_directors=[{'name': 'Villeneuve', 'strength': 'high',
                        'url': '/director/42'}]))
    assert page.eval_on_selector('#taste-dna-directors a', 'e => e.tagName') \
        == 'A'
    assert page.eval_on_selector('#taste-dna-directors a',
                                 'e => e.getAttribute("href")') == '/director/42'
    assert not page.f19_errors


def test_browser_unlinked_director_is_plain_text(taste_browser):
    page = _render_chromium(taste_browser, _base_payload(
        directors_status='ready',
        top_directors=[{'name': 'Ambiguous Person', 'strength': 'high'}]))
    assert page.eval_on_selector_all('#taste-dna-directors a',
                                     'e => e.length') == 0
    assert 'Ambiguous Person' in page.inner_text('#taste-dna-directors')
    assert not page.f19_errors


def test_browser_director_link_is_keyboard_focusable(taste_browser):
    page = _render_chromium(taste_browser, _base_payload(
        directors_status='ready',
        top_directors=[{'name': 'Villeneuve', 'strength': 'high',
                        'url': '/director/42'}]))
    assert page.eval_on_selector('#taste-dna-directors a',
                                 'e => e.tabIndex') >= 0
    page.focus('#taste-dna-directors a')
    assert page.evaluate('document.activeElement.tagName') == 'A'


def test_browser_mixed_linked_and_unlinked(taste_browser):
    page = _render_chromium(taste_browser, _base_payload(
        directors_status='ready',
        top_directors=[{'name': 'Linked', 'strength': 'high',
                        'url': '/director/7'},
                       {'name': 'Unlinked', 'strength': 'low'}]))
    assert page.eval_on_selector_all('#taste-dna-directors a',
                                     'e => e.length') == 1
    text = page.inner_text('#taste-dna-directors')
    assert 'Linked' in text and 'Unlinked' in text


def test_browser_f18_empty_states_still_render(taste_browser):
    for status, expect in (
            ('no_director_data', 'Director credits'),
            ('no_positive_directors', 'No directors stand out yet')):
        page = _render_chromium(taste_browser,
                                _base_payload(directors_status=status))
        assert page.is_visible('#taste-dna-directors-empty-block')
        assert expect in page.inner_text('#taste-dna-directors-empty')
        assert not page.f19_errors
        page.close()
