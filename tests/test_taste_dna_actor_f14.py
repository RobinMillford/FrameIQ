"""Feature F14 — actor affinity exposed through the private Taste DNA.

F9 persisted cast (Person/MediaCast) and added an `actor_affinity` dimension to
the taste profile, but the dimension was computed and stored and never shown.
These tests pin the presentation contract only; the computation, the signed
evidence and the L2 normalization are F9's and stay covered by
test_cast_capture.py.

The central design decision is asserted here: the API exposes a ready-made
relative `/actor/<id>` URL, NOT a raw person id, because the presentation model
is contractually id-free (test_taste_profile_api.test_no_internal_ids_returned
asserts `'tmdb' not in body`). Identity resolution is unambiguous-only, so a
name collision or a rename yields an unlinked entry rather than a wrong link.

Harness notes: `app` is session-scoped and the DB is NOT rolled back per test,
so every user/person handle is uniquified and every test gets its own app
context. That is why helpers return captured values, not live ORM instances.
"""
import re
import uuid

import pytest

import api.taste_profile as tp


# ── helpers ─────────────────────────────────────────────────────────────────

_SEQ = {'n': 900_000_000}


def _uniq(prefix):
    return '%s_%s' % (prefix, uuid.uuid4().hex[:8])


def _next_tmdb_id():
    _SEQ['n'] += 1
    return _SEQ['n']


@pytest.fixture(autouse=True)
def _ctx(app):
    """Every test touches the ORM; give each one its own app context."""
    with app.app_context():
        yield


def _make_user(prefix='f14user'):
    from models import User, db as _db
    username = _uniq(prefix)
    u = User(username=username, email=f'{username}@example.com',
             email_verified=True)
    u.set_password('TestPass1')
    _db.session.add(u)
    _db.session.commit()
    return u.id, username


def _person(name=None):
    """Create a persisted Person and return its ACTUAL (name, tmdb id).

    The name is ALWAYS uniquified: the DB is never rolled back, so a repeated
    literal name would make a later test's name ambiguous and silently change
    what it is asserting.
    """
    from models.cast import Person, db as _db
    p = Person(name=_uniq(name or 'Actor'),
               tmdb_person_id=_next_tmdb_id(), source='tmdb')
    _db.session.add(p)
    _db.session.commit()
    return p.name, p.tmdb_person_id


def _profile(uid, *, genres=None, actors=None, titles=10):
    from models import TasteProfile, db as _db
    p = TasteProfile(user_id=uid)
    p.genre_weights = genres or {'Drama': 0.9}
    p.actor_affinity = actors or {}
    p.confidence = 0.7
    p.distinct_title_count = titles
    p.signal_count = 20
    _db.session.add(p)
    _db.session.commit()
    return p


@pytest.fixture
def user():
    return _make_user()


@pytest.fixture
def auth_client(client, user):
    uid, username = user
    client.post('/login', data={'username': username,
                                'password': 'TestPass1'})
    return client


def _get(client):
    return client.get('/api/taste-profile').get_json()


def _count_person_queries():
    from sqlalchemy import event
    from models import db as _db
    counter = {'n': 0, 'sql': []}

    def _count(_conn, _cur, statement, *a, **k):
        flat = ' '.join(statement.split()).lower()
        if ' from person' in flat:
            counter['n'] += 1
            counter['sql'].append(flat[:90])

    event.listen(_db.engine, 'before_cursor_execute', _count)
    counter['_engine'] = _db.engine
    counter['_handler'] = _count
    return counter


def _stop(counter):
    from sqlalchemy import event
    event.remove(counter['_engine'], 'before_cursor_execute',
                 counter['_handler'])


# ── resolve_actor_links: identity safety ────────────────────────────────────

def test_resolves_unambiguous_name_to_actor_url():
    name, tmdb_id = _person('Cillian Murphy')
    assert tp.resolve_actor_links([name]) == {name: '/actor/%d' % tmdb_id}


def test_unknown_name_resolves_to_nothing():
    """Renamed / unenriched: no row exists, so no link may be invented."""
    assert tp.resolve_actor_links([_uniq('GhostActor')]) == {}


def test_ambiguous_name_is_never_linked():
    """Two persisted people sharing a display name must resolve to NEITHER.

    Linking either would point the user at the wrong person, which is worse
    than showing no link at all.
    """
    from models.cast import Person, db as _db
    shared = _uniq('AlexSmith')
    for _ in range(2):
        _db.session.add(Person(name=shared, tmdb_person_id=_next_tmdb_id(),
                               source='tmdb'))
    _db.session.commit()
    assert tp.resolve_actor_links([shared]) == {}


def test_rename_is_not_fabricated():
    """Affinity holds the OLD name; the row holds the NEW one -> no link.

    This is the real F9 limitation: the persisted dimension is keyed by display
    name, so a rename breaks the join until the next recompute. We fail closed.
    """
    _person('Current Name')
    assert tp.resolve_actor_links([_uniq('PreviousName')]) == {}


def test_resolve_is_one_batched_query():
    """Cost does not scale with actor count -> no N+1."""
    from sqlalchemy import event
    from models import db as _db
    names = []
    for _ in range(6):
        names.append(_person()[0])
    count = {'n': 0}

    def _count(*a, **k):
        count['n'] += 1
    event.listen(_db.engine, 'before_cursor_execute', _count)
    try:
        links = tp.resolve_actor_links(names)
    finally:
        event.remove(_db.engine, 'before_cursor_execute', _count)
    assert len(links) == 6
    assert count['n'] == 1


def test_resolve_empty_input_costs_no_query():
    from sqlalchemy import event
    from models import db as _db
    count = {'n': 0}

    def _count(*a, **k):
        count['n'] += 1
    event.listen(_db.engine, 'before_cursor_execute', _count)
    try:
        assert tp.resolve_actor_links([]) == {}
        assert tp.resolve_actor_links(None) == {}
        assert tp.resolve_actor_links(['', None]) == {}
    finally:
        event.remove(_db.engine, 'before_cursor_execute', _count)
    assert count['n'] == 0


# ── taste_dna: presentation contract ────────────────────────────────────────

def test_actor_entries_mirror_director_shape():
    entries = tp._actor_entries({'A': 0.9, 'B': 0.5}, {})
    assert entries == [
        {'name': 'A', 'strength': 'high'},
        {'name': 'B', 'strength': 'moderate'},
    ]


def test_actor_entries_include_url_when_resolved():
    assert tp._actor_entries({'A': 0.9}, {'A': '/actor/77'}) == [
        {'name': 'A', 'strength': 'high', 'url': '/actor/77'}]


def test_actor_entries_unlinked_when_identity_is_ambiguous():
    """No url key at all — never a wrong link, never an inert href."""
    assert 'url' not in tp._actor_entries({'A': 0.9}, {})[0]


def test_actor_entries_ignore_foreign_url_schemes():
    """A resolver bug must not be able to inject an external href."""
    for bad in ('https://evil.example/x', 'javascript:alert(1)',
                '/actor/', '/actor/abc', 42, None):
        assert 'url' not in tp._actor_entries({'A': 0.9}, {'A': bad})[0]


def test_actor_entries_are_bounded_and_positive_only():
    weights = {'Actor %02d' % i: 0.5 for i in range(20)}
    weights['Disliked'] = -0.9
    entries = tp._actor_entries(weights, {})
    assert len(entries) == tp.DNA_TOP_ACTORS
    assert 'Disliked' not in [e['name'] for e in entries]


def test_actor_entries_are_deterministic():
    weights = {'B': 0.5, 'A': 0.5, 'C': 0.5}
    assert tp._actor_entries(weights, {}) == tp._actor_entries(weights, {})


def test_actor_entries_ignore_malformed_input():
    assert tp._actor_entries({'Good': 0.8}, {}) == [
        {'name': 'Good', 'strength': 'high'}]
    assert tp._actor_entries(None, {}) == []
    assert tp._actor_entries({}, None) == []


def test_actors_status_distinguishes_all_three_states():
    """An empty list has two different causes and they need different words."""
    # actors displayed
    assert tp._actors_status([{'name': 'A'}], 10, {'A': 0.9}) == 'ready'
    # dimension RAN and produced evidence, but nothing scored positive:
    # the cast metadata EXISTS. Must not be reported as missing metadata.
    assert tp._actors_status([], 10, {'A': -0.9}) == 'no_qualifying_actors'
    # dimension holds no evidence at all: cast metadata is unenriched/limited
    assert tp._actors_status([], 10, {}) == 'no_cast'
    # nothing watched at all -> no section, no claim
    assert tp._actors_status([], 0, {}) is None
    assert tp._actors_status([], 0, {'A': -1.0}) is None


# ── API surface ─────────────────────────────────────────────────────────────

def test_api_exposes_actors_with_working_links(auth_client, user):
    name, tmdb_id = _person('Cillian Murphy')
    _profile(user[0], actors={name: 0.9})
    body = _get(auth_client)
    assert body['actors_status'] == 'ready'
    assert body['top_actors'] == [
        {'name': name, 'strength': 'high', 'url': '/actor/%d' % tmdb_id}]


def test_api_actor_link_targets_the_existing_route(auth_client, user):
    name, tmdb_id = _person('Someone')
    _profile(user[0], actors={name: 0.8})
    url = _get(auth_client)['top_actors'][0]['url']
    assert url == '/actor/%d' % tmdb_id
    from app import create_app
    rules = {r.rule for r in create_app().url_map.iter_rules()}
    assert '/actor/<int:actor_id>' in rules


def test_api_unenriched_state_is_honest(auth_client, user):
    """Watched titles, no persisted cast evidence: say so, don't blame."""
    _profile(user[0], actors={}, titles=12)
    body = _get(auth_client)
    assert body['actors_status'] == 'no_cast'
    assert 'top_actors' not in body


def test_api_negative_only_evidence_is_not_reported_as_missing_metadata(
        auth_client, user):
    """Cast evidence exists but nothing is positive -> a real result.

    Reporting this as 'no_cast' would tell the user their cast metadata is
    missing when it demonstrably is not.
    """
    _profile(user[0], actors={'Some Actor': -0.8}, titles=12)
    body = _get(auth_client)
    assert body['actors_status'] == 'no_qualifying_actors'
    assert 'top_actors' not in body


def test_api_ambiguous_actor_is_shown_without_a_link(auth_client, user):
    from models.cast import Person, db as _db
    uid, _ = user
    shared = _uniq('AlexSmith')
    for _ in range(2):
        _db.session.add(Person(name=shared, tmdb_person_id=_next_tmdb_id(),
                               source='tmdb'))
    _db.session.commit()
    _profile(uid, actors={shared: 0.9})
    body = _get(auth_client)
    assert body['top_actors'][0]['name'] == shared
    assert 'url' not in body['top_actors'][0]


def test_api_renamed_actor_is_shown_without_a_bogus_link(auth_client, user):
    _person('Current Name')
    _profile(user[0], actors={'Previous Name': 0.9})
    body = _get(auth_client)
    assert body['top_actors'][0]['name'] == 'Previous Name'
    assert 'url' not in body['top_actors'][0]


def test_api_malformed_affinity_does_not_break_the_page(auth_client, user):
    from models import TasteProfile, db as _db
    uid, _ = user
    p = TasteProfile(user_id=uid)
    p.genre_weights = {'Drama': 0.9}
    p.actor_affinity_json = 'not json at all'
    p.confidence = 0.7
    p.distinct_title_count = 5
    _db.session.add(p)
    _db.session.commit()
    body = _get(auth_client)
    assert body['available'] is True
    assert body['actors_status'] == 'no_cast'


def test_api_actor_payload_leaks_no_watch_history(auth_client, user):
    name, _ = _person()
    _profile(user[0], actors={name: 0.9})
    text = str(_get(auth_client)).lower()
    for leaked in ('watched', 'watchlist', 'diary', 'media_type',
                   'review', 'rating', 'signal_count', 'distinct_title'):
        assert leaked not in text


def test_api_never_emits_raw_weights_or_ids(auth_client, user):
    name, tmdb_id = _person()
    _profile(user[0], actors={name: 0.8123})
    text = str(_get(auth_client))
    assert '0.8123' not in text
    assert 'tmdb' not in text.lower()
    # The id may appear ONLY as the /actor/<id> link target — never as a bare
    # field. Compare against the response with the link stripped, rather than
    # substring-matching digits (the fixture name is uuid hex and may contain
    # any digit, which makes a bare-digit assertion flaky).
    without_link = text.replace('/actor/%d' % tmdb_id, '')
    assert str(tmdb_id) not in without_link
    # Assert the response SHAPE rather than digit substrings: the fixture name
    # is uuid hex and the link holds a 9-digit id, so any bare-digit check is
    # a coin flip. What matters is that no identity field is exposed.
    assert 'person_id' not in text
    assert 'user_id' not in text
    assert 'profile_version' not in text
    for entry in _get(auth_client)['top_actors']:
        assert set(entry) <= {'name', 'strength', 'url'}


def test_api_actor_section_is_additive(auth_client, user):
    name, _ = _person()
    _profile(user[0], genres={'Drama': 0.9, 'Thriller': 0.7},
             actors={name: 0.9})
    body = _get(auth_client)
    # Every pre-existing key survives with its original meaning.
    for key in ('available', 'level', 'confidence', 'titles_analyzed',
                'top_genres'):
        assert key in body
    assert [g['name'] for g in body['top_genres']] == ['Drama', 'Thriller']
    # Sections with no evidence are still omitted (unchanged compact rule).
    assert 'avoid_genres' not in body
    assert 'top_directors' not in body
    # Additive only.
    assert body['actors_status'] == 'ready'
    assert len(body['top_actors']) == 1


def test_api_cold_start_when_only_actor_evidence_exists(auth_client, user):
    from models import TasteProfile, db as _db
    p = TasteProfile(user_id=user[0])
    p.genre_weights = {}
    p.actor_affinity = {'Nobody': 0.9}
    _db.session.add(p)
    _db.session.commit()
    assert _get(auth_client) == {'available': False, 'state': 'cold_start'}


def test_api_cold_start_when_no_profile_at_all(auth_client):
    assert _get(auth_client) == {'available': False, 'state': 'cold_start'}


def test_api_never_recomputes_the_dimension(auth_client, user, monkeypatch):
    """F14 is presentation only; compute_profile must never be invoked."""
    name, _ = _person()
    _profile(user[0], actors={name: 0.9})
    calls = []
    monkeypatch.setattr(tp, 'compute_profile',
                        lambda *a, **k: calls.append(1))
    assert _get(auth_client)['actors_status'] == 'ready'
    assert calls == []


def test_api_does_no_tmdb_network_call(auth_client, user, monkeypatch):
    import socket

    def _no_network(*a, **k):
        raise AssertionError('F14 must not hit the network')

    monkeypatch.setattr(socket, 'socket', _no_network)
    monkeypatch.setattr(socket, 'create_connection', _no_network)
    name, _ = _person()
    _profile(user[0], actors={name: 0.9})
    assert _get(auth_client)['actors_status'] == 'ready'


def test_api_query_count_is_bounded_and_flat(auth_client, user):
    """One profile read + exactly one batched person lookup."""
    names = [_person()[0] for _ in range(5)]
    _profile(user[0], actors={n: 0.9 - i * 0.1 for i, n in enumerate(names)})
    c = _count_person_queries()
    try:
        body = _get(auth_client)
    finally:
        _stop(c)
    assert len(body['top_actors']) == 5
    # Exactly ONE person lookup for five actors -> flat, not N+1.
    assert c['n'] == 1, c['sql']


def test_api_no_extra_query_without_actor_data(auth_client, user):
    """The common case (no actor affinity) costs nothing extra."""
    _profile(user[0], actors={}, titles=5)
    c = _count_person_queries()
    try:
        assert _get(auth_client)['actors_status'] == 'no_cast'
    finally:
        _stop(c)
    # resolve_actor_links short-circuits on an empty name set, so F14 adds NO
    # person lookup at all for the common case.
    assert c['n'] == 0, c['sql']


# ── privacy / access control ────────────────────────────────────────────────

def test_cannot_read_another_users_actor_affinity(client, user):
    uid, username = user
    my_name, _ = _person('Mine')
    other_name, _ = _person('OtherPersonsActor')
    _profile(uid, actors={my_name: 0.9})
    other_id, _ = _make_user('f14other')
    _profile(other_id, actors={other_name: 0.9})
    client.post('/login', data={'username': username,
                                'password': 'TestPass1'})
    body = str(_get(client))
    assert my_name in body          # our own, yes
    assert other_name not in body   # theirs, never


def test_anonymous_cannot_read_taste_dna(client):
    r = client.get('/api/taste-profile')
    assert r.status_code in (302, 401)


def test_user_id_parameter_cannot_target_another_user(auth_client, user):
    mine, _ = _person()
    theirs, _ = _person()
    _profile(user[0], actors={mine: 0.9})
    other_id, _ = _make_user('f14other2')
    _profile(other_id, actors={theirs: 0.9})
    body = auth_client.get(
        '/api/taste-profile?user_id=%d' % other_id).get_json()
    # We see OUR affinity, never theirs.
    assert [a['name'] for a in body['top_actors']] == [mine]


def test_actor_affinity_absent_from_public_metadata(auth_client, user):
    """F13's privacy default is untouched by F14."""
    secret, _ = _person('SecretActor')
    _profile(user[0], actors={secret: 0.9})
    page = auth_client.get('/profile').data.decode()
    head = page.split('</head>')[0]
    assert secret not in page
    assert secret not in head
    assert 'noindex, nofollow' in head


# ── UI ──────────────────────────────────────────────────────────────────────

def _read(path):
    with open(path, encoding='utf-8') as fh:
        return fh.read()


def test_template_has_actor_section_and_empty_state():
    html = _read('templates/profile.html')
    assert 'id="taste-dna-actors"' in html
    assert 'id="taste-dna-actors-block"' in html
    assert 'id="taste-dna-actors-empty-block"' in html
    assert 'Actors in your taste' in html


def test_template_preserves_every_existing_dimension():
    html = _read('templates/profile.html')
    for keep in ('taste-dna-genres', 'taste-dna-avoid',
                 'taste-dna-directors', 'taste-dna-eras',
                 'taste-dna-media', 'taste-dna-runtime',
                 'taste-dna-level', 'taste-dna-basis'):
        assert keep in html


def test_actor_blocks_start_hidden():
    import re as _re
    html = _read('templates/profile.html')
    for block in ('taste-dna-actors-block', 'taste-dna-actors-empty-block'):
        tag = _re.search(r'<div[^>]*id="%s"[^>]*>' % block, html).group(0)
        assert 'hidden' in tag


def test_empty_state_copy_lives_in_one_placeholder():
    """The paragraph is filled per status by the JS, not hardcoded."""
    html = _read('templates/profile.html')
    start = html.index('taste-dna-actors-empty"')
    tag = re.search(r'<p[^>]*id="taste-dna-actors-empty"[^>]*>(.*?)</p>',
                    html, re.S).group(1)
    assert tag.strip() == '', 'copy should be set by the JS per status'
    assert start > 0


def test_js_defines_distinct_copy_per_empty_state():
    js = _read('static/js/taste-dna.js')
    assert 'ACTOR_EMPTY_TEXT' in js
    assert 'no_cast:' in js
    assert 'no_qualifying_actors:' in js
    # Neither message may blame the user's preferences.
    for phrase in re.findall(r"'(Actor insights|No actors)[^']*'", js):
        assert 'preferences' not in phrase.lower()


def test_js_renders_links_only_when_url_present():
    js = _read('static/js/taste-dna.js')
    chip = js[js.index('function actorChip'):js.index('function fillActors')]
    assert chip.index('if (item.url)') < chip.index("createElement('a')")


def test_js_shows_empty_state_only_for_known_statuses():
    js = _read('static/js/taste-dna.js')
    # A lookup table keyed by actors_status: an unknown/absent status finds no
    # message and the section stays hidden rather than claiming anything.
    assert 'ACTOR_EMPTY_TEXT[status]' in js
    assert 'if (!message) {' in js
    assert "show('taste-dna-actors-empty-block')" in js


def test_js_actor_links_are_keyboard_reachable():
    js = _read('static/js/taste-dna.js')
    chip = js[js.index('function actorChip'):js.index('function fillActors')]
    assert "createElement('a')" in chip
    assert 'focus-visible:ring' in chip
    assert 'hover:underline' in chip
    # A real anchor is natively focusable; no tabindex juggling.
    assert 'tabIndex' not in chip
    assert 'role="link"' not in chip


def test_js_resets_state_so_stale_sections_cannot_persist():
    js = _read('static/js/taste-dna.js')
    fill = js[js.index('function fillActors'):js.index('function mediaSentence')]
    assert fill.index("classList.add('hidden')") < fill.index('show(')


def test_js_preserves_existing_sections():
    js = _read('static/js/taste-dna.js')
    for keep in ('data.top_genres', 'data.avoid_genres',
                 'data.top_directors', 'data.eras',
                 'data.media_preference', 'data.runtime',
                 'data.titles_analyzed'):
        assert keep in js


def test_js_defends_against_malformed_entries():
    js = _read('static/js/taste-dna.js')
    fill = js[js.index('function fillActors'):js.index('function mediaSentence')]
    assert 'item && item.name' in fill