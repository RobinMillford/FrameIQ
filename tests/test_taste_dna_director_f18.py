"""Feature F18 — honest director-affinity state in Taste DNA.

Scope note, and the reason this suite is small. Director affinity has been
computed and DISPLAYED since Phase 10: ``director_affinity_json`` is already
persisted, ``taste_dna()`` already returns ``top_directors``, and the profile
already renders a "Directors you watch" block. F18 deliberately does NOT
rebuild any of that.

What was genuinely missing is an honest reason for the section being blank.
A user whose titles were processed by the historically-broken director
enrichment (fixed in F16) has a populated profile but zero persisted director
rows, so the block simply vanished — with no explanation and no implication
either way about their taste. F18 adds only:

* ``directors_status`` — an additive, pure signal mirroring F14's
  ``actors_status``; and
* a neutral empty state in the UI.

These tests also pin the honesty constraints: no internal ids, no cross-user
leakage, no extra query per director, no network, and copy that never claims
the user's director preferences are absent.
"""
import re
import uuid

import pytest

import api.taste_profile as tp
from models import db


# ── helpers ────────────────────────────────────────────────────────────────

_SEQ = {'n': 9_500_000}


def _uniq(prefix):
    return '%s_%s' % (prefix, uuid.uuid4().hex[:8])


@pytest.fixture(autouse=True)
def _ctx(app):
    """Every test touches the ORM; give each its own app context."""
    with app.app_context():
        yield


def _person(name=None, tmdb_person_id=None):
    from models.cast import Person, db as _db
    p = Person(name=name or _uniq('Dir'),
               tmdb_person_id=tmdb_person_id or _next_id(), source='tmdb')
    _db.session.add(p)
    _db.session.commit()
    return p


def _next_id():
    _SEQ['n'] += 1
    return _SEQ['n']


def _profile(uid, *, genres=None, directors=None, titles=12):
    from models import TasteProfile, db as _db
    p = TasteProfile(user_id=uid)
    p.genre_weights = genres or {'Drama': 0.9}
    p.director_affinity = directors or {}
    p.confidence = 0.7
    p.distinct_title_count = titles
    p.signal_count = 20
    _db.session.add(p)
    _db.session.commit()
    return p


@pytest.fixture
def user():
    from models import User, db as _db
    username = _uniq('f18user')
    u = User(username=username, email='%s@example.com' % username,
             email_verified=True)
    u.set_password('TestPass1')
    _db.session.add(u)
    _db.session.commit()
    return u.id, username


@pytest.fixture
def auth_client(client, user):
    uid, username = user
    client.post('/login', data={'username': username,
                                'password': 'TestPass1'})
    return client


def _get(client):
    return client.get('/api/taste-profile').get_json()


def _read(path):
    with open(path, encoding='utf-8') as fh:
        return fh.read()


# ── pure status logic ───────────────────────────────────────────────────────

def test_directors_status_has_three_states():
    """An empty display does NOT mean 'no data'.

    A non-empty affinity map that yields no qualifying entry means director
    evidence EXISTS and simply has nothing positive — a different fact from
    having no evidence at all, and it needs different words.
    """
    assert tp._directors_status([{'name': 'A'}], 12, {'A': 1.0}) == 'ready'
    assert tp._directors_status([], 12, {}) == 'no_director_data'
    assert (tp._directors_status([], 12, {'A': -0.8})
            == 'no_positive_directors')
    assert tp._directors_status([], 0, {}) is None
    assert tp._directors_status([], 0, {'A': -0.8}) is None


def test_negative_only_does_not_get_the_missing_credits_status(
        auth_client, user):
    """The review gate: never claim credits are missing when they were read."""
    _profile(user[0], directors={'Villeneuve': -0.8}, titles=12)
    body = _get(auth_client)
    assert body['directors_status'] == 'no_positive_directors'
    assert 'top_directors' not in body


def test_directors_status_never_reads_directors_enriched_at():
    """The F16 defect means that column proves nothing about persistence.

    Guard against a future refactor reintroducing it as coverage evidence.
    """
    import ast
    import inspect
    src = inspect.getsource(tp._directors_status)
    # Strip the docstring: it explains WHY the column is avoided, so the name
    # legitimately appears there. What must not exist is actual usage.
    tree = ast.parse(src.lstrip())
    fn = tree.body[0]
    if (fn.body and isinstance(fn.body[0], ast.Expr)
            and isinstance(fn.body[0].value, ast.Constant)):
        fn.body = fn.body[1:]
    code = ast.dump(ast.Module(body=fn.body, type_ignores=[]))
    assert 'directors_enriched_at' not in code
    assert 'cast_enriched_at' not in code


# ── API ─────────────────────────────────────────────────────────────────────

def test_api_reports_ready_when_directors_exist(auth_client, user):
    _profile(user[0], directors={'Denis Villeneuve': 0.9})
    body = _get(auth_client)
    assert body['directors_status'] == 'ready'
    assert [d['name'] for d in body['top_directors']] == ['Denis Villeneuve']


def test_api_reports_no_positive_directors_distinctly(auth_client, user):
    """Director evidence present but nothing positive -> its own status."""
    _profile(user[0], directors={'Villeneuve': -0.8}, titles=12)
    body = _get(auth_client)
    assert body['directors_status'] == 'no_positive_directors'
    assert 'top_directors' not in body


def test_api_reports_no_data_for_the_historical_gap(auth_client, user):
    """Watched titles, zero director rows -> an explicit, honest status."""
    _profile(user[0], directors={}, titles=25)
    body = _get(auth_client)
    assert body['directors_status'] == 'no_director_data'
    assert 'top_directors' not in body


def test_api_cold_start_omits_the_status(auth_client):
    """No profile at all -> cold start, no director claim."""
    assert _get(auth_client) == {'available': False, 'state': 'cold_start'}


def test_api_directors_remain_bounded_and_ordered(auth_client, user):
    many = {'Dir %02d' % i: 0.5 for i in range(20)}
    _profile(user[0], directors=many)
    body = _get(auth_client)
    assert len(body['top_directors']) == tp.DNA_TOP_DIRECTORS
    # Deterministic: weight desc, then name.
    names = [d['name'] for d in body['top_directors']]
    assert names == sorted(names)


def test_api_excludes_negative_director_evidence(auth_client, user):
    _profile(user[0], directors={'Liked': 0.9, 'Disliked': -0.8})
    body = _get(auth_client)
    assert [d['name'] for d in body['top_directors']] == ['Liked']


def test_api_is_additive_and_preserves_every_existing_key(auth_client, user):
    _profile(user[0], genres={'Drama': 0.9, 'Thriller': 0.7},
             directors={'Villeneuve': 0.9})
    body = _get(auth_client)
    for key in ('available', 'level', 'confidence', 'titles_analyzed',
                'top_genres', 'top_directors', 'directors_status'):
        assert key in body, key
    # Section-with-no-evidence omission rule is unchanged.
    assert 'avoid_genres' not in body
    assert 'eras' not in body
    assert [g['name'] for g in body['top_genres']] == ['Drama', 'Thriller']


def test_api_actor_insights_are_unchanged(auth_client, user):
    """F18 must not touch the actor dimension."""
    person = _person('Someone')
    _profile(user[0], directors={'Villeneuve': 0.9})
    from models import TasteProfile
    p = TasteProfile.query.filter_by(user_id=user[0]).one()
    p.actor_affinity = {person.name: 0.9}
    db.session.commit()
    body = _get(auth_client)
    assert body['actors_status'] == 'ready'
    assert [a['name'] for a in body['top_actors']] == [person.name]


def test_api_director_entry_shape_is_unchanged(auth_client, user):
    """Phase 10 shape preserved - no ids smuggled into a director entry."""
    _profile(user[0], directors={'Villeneuve': 0.9})
    for entry in _get(auth_client)['top_directors']:
        assert set(entry) == {'name', 'strength'}


def test_api_malformed_affinity_does_not_break_the_page(auth_client, user):
    from models import TasteProfile
    p = TasteProfile(user_id=user[0])
    p.genre_weights = {'Drama': 0.9}
    p.director_affinity_json = 'not json'
    p.confidence = 0.7
    p.distinct_title_count = 10
    db.session.add(p)
    db.session.commit()
    body = _get(auth_client)
    assert body['available'] is True
    assert body['directors_status'] == 'no_director_data'


def test_api_exposes_no_internal_ids(auth_client, user):
    _profile(user[0], directors={'Villeneuve': 0.8123})
    person = _person('Villeneuve')
    text = str(_get(auth_client))
    assert '0.8123' not in text
    assert 'tmdb' not in text.lower()
    assert 'user_id' not in text
    assert 'profile_version' not in text
    for entry in _get(auth_client)['top_directors']:
        assert set(entry) == {'name', 'strength'}
    assert str(person.tmdb_person_id) not in text


def test_api_leaks_no_viewing_history(auth_client, user):
    _profile(user[0], directors={'Villeneuve': 0.9})
    text = str(_get(auth_client)).lower()
    for leaked in ('watched', 'watchlist', 'diary', 'review', 'rating',
                   'signal_count', 'distinct_title'):
        assert leaked not in text


def test_api_cannot_read_another_users_directors(client, user):
    mine = 'Mine Director'
    theirs = 'Their Director'
    _profile(user[0], directors={mine: 0.9})
    other_id, other_username = _make_user()
    _profile(other_id, directors={theirs: 0.9})
    client.post('/login', data={'username': other_username,
                                'password': 'TestPass1'})
    body = str(_get(client))
    assert theirs in body
    assert mine not in body


def _make_user():
    from models import User
    username = _uniq('f18other')
    u = User(username=username, email='%s@example.com' % username,
             email_verified=True)
    u.set_password('TestPass1')
    db.session.add(u)
    db.session.commit()
    return u.id, username


def test_api_does_no_network_call(auth_client, user, monkeypatch):
    import socket

    def _no_network(*a, **k):
        raise AssertionError('F18 must not hit the network')
    monkeypatch.setattr(socket, 'socket', _no_network)
    monkeypatch.setattr(socket, 'create_connection', _no_network)
    _profile(user[0], directors={'Villeneuve': 0.9})
    assert _get(auth_client)['directors_status'] == 'ready'


def test_api_director_link_lookup_is_batched(auth_client, user):
    """F19 (was: F18 adds no query) — now exactly ONE lookup, never per chip.

    F18 needed no query at all. F19 resolves displayed names to local Director
    rows, which legitimately costs one batched statement. The property that
    matters is that the count does NOT scale with the number of directors.
    """
    from sqlalchemy import event

    from models import TasteProfile
    from models.director import Director
    _profile(user[0], directors={'Villeneuve': 0.9})
    for i in range(5):
        d = Director(name='Dir %d' % i, tmdb_person_id=9_400_000 + i)
        db.session.add(d)
    db.session.commit()
    directors = [d.name for d in Director.query.all()][:5]
    p = TasteProfile.query.filter_by(user_id=user[0]).one()
    p.director_affinity = {n: 0.9 - i * 0.1 for i, n in enumerate(directors)}
    db.session.commit()

    statements = []

    def _record(_conn, _cur, stmt, *a, **k):
        statements.append(' '.join(stmt.split()).lower())
    event.listen(db.engine, 'before_cursor_execute', _record)
    try:
        body = _get(auth_client)
    finally:
        event.remove(db.engine, 'before_cursor_execute', _record)
    lookups = [s for s in statements if 'from director' in s]
    # One statement for five displayed directors -> flat, not N+1.
    assert len(lookups) == 1, lookups
    assert len(body['top_directors']) == 5


# ── UI ──────────────────────────────────────────────────────────────────────

def test_template_has_directors_empty_state():
    html = _read('templates/profile.html')
    assert 'id="taste-dna-directors-empty-block"' in html
    assert 'id="taste-dna-directors-empty"' in html


def test_template_preserves_the_existing_directors_block():
    html = _read('templates/profile.html')
    assert 'id="taste-dna-directors"' in html
    assert 'Directors you watch' in html
    for keep in ('taste-dna-genres', 'taste-dna-avoid', 'taste-dna-actors',
                 'taste-dna-actors-empty-block', 'taste-dna-eras',
                 'taste-dna-media', 'taste-dna-runtime'):
        assert keep in html


def test_directors_blocks_start_hidden():
    html = _read('templates/profile.html')
    for block in ('taste-dna-directors-block',
                  'taste-dna-directors-empty-block'):
        tag = re.search(r'<div[^>]*id="%s"[^>]*>' % block, html).group(0)
        assert 'hidden' in tag


def test_empty_state_paragraph_is_js_filled():
    """No hardcoded copy that could go stale or contradict the status."""
    html = _read('templates/profile.html')
    tag = re.search(r'<p[^>]*id="taste-dna-directors-empty"[^>]*>(.*?)</p>',
                    html, re.S).group(1)
    assert tag.strip() == ''


def test_js_renders_directors_and_the_empty_state():
    js = _read('static/js/taste-dna.js')
    assert 'function fillDirectors' in js
    assert 'DIRECTOR_EMPTY_TEXT[status]' in js
    # F19: chips now go through directorChip(), which links when a url is
    # present and renders plain text otherwise.
    assert 'function directorChip' in js
    assert 'directorChip(item)' in js
    assert 'data-taste-director-link' in js


def test_js_resets_both_director_blocks():
    """A re-render must never leave a stale section behind."""
    js = _read('static/js/taste-dna.js')
    body = js[js.index('function fillDirectors'):
              js.index('function mediaSentence')]
    assert body.count("classList.add('hidden')") >= 2


def test_js_copy_makes_no_claim_about_the_users_taste():
    js = _read('static/js/taste-dna.js')
    body = js[js.index('var DIRECTOR_EMPTY_TEXT'):
              js.index('function mediaSentence')]
    lowered = body.lower()
    for forbidden in ('no director preferences', 'never watched',
                      "don't like directors"):
        assert forbidden not in lowered


def test_js_has_distinct_copy_per_state():
    """The two empty states must say different things.

    The credits-missing wording belongs ONLY to the no-evidence case: telling a
    user whose director evidence exists that their credits are unavailable
    would be a claim the data contradicts.
    """
    js = _read('static/js/taste-dna.js')
    body = js[js.index('var DIRECTOR_EMPTY_TEXT'):
              js.index('function mediaSentence')]
    assert 'no_director_data:' in body
    assert 'no_positive_directors:' in body
    credits_msg = body[body.index('no_director_data:'):
                       body.index('no_positive_directors:')]
    positive_msg = body[body.index('no_positive_directors:'):]
    assert 'Director credits aren' in credits_msg
    assert 'Director credits aren' not in positive_msg
    assert 'No directors stand out yet' in positive_msg


def test_js_preserves_every_existing_dimension():
    js = _read('static/js/taste-dna.js')
    for keep in ('data.top_genres', 'data.avoid_genres', 'data.eras',
                 'data.media_preference', 'data.runtime',
                 'data.titles_analyzed', 'fillActors('):
        assert keep in js


def test_unknown_status_stays_silent():
    js = _read('static/js/taste-dna.js')
    body = js[js.index('function fillDirectors'):
              js.index('function mediaSentence')]
    assert 'if (!message)' in body
    assert 'return;' in body
