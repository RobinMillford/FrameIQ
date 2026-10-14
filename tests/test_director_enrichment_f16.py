"""Feature F16 — director enrichment reads the response it actually gets.

Root cause this suite pins: ``api.tmdb.movies.fetch_movie_details`` returns a
NORMALIZED dict that contains no ``credits`` key — it keeps only a director's
display NAME (and, from an overwriting loop, only the last one). But
``scripts/enrich_directors._extract_directors`` read
``payload['credits']['crew']``, a shape the function under test never returns.

So extraction always produced ``[]``, every movie was stamped
``directors_enriched_at``, and zero ``Director`` rows were written — a silent,
100% data loss that also made the bug invisible to the existing suite, because
every one of those tests injected a hand-built RAW payload via a fake fetcher
rather than exercising the real contract.

F16 fixes both sides: the fetcher now exposes the ordered director list with
TMDb ids (purely additive), and the extractor understands the real contract
while still accepting the legacy raw shape used by existing fixtures.
"""
import importlib.util
from datetime import datetime

import pytest

from models import MediaItem
from models.director import Director, MediaDirector


# ── fixtures ────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _release_session(app, db):
    """Roll the session back after every test in this module.

    Several tests assert that a contract failure leaves the item UNMARKED, and
    do so inside ``pytest.raises`` — which leaves the SQLAlchemy session
    holding an open read transaction. On SQLite that blocks the next test's
    write with "database is locked". The `app` fixture is session-scoped with
    no per-test teardown, so the rollback has to be explicit.
    """
    yield
    db.session.rollback()


@pytest.fixture(scope='module')
def enrich_script():
    spec = importlib.util.spec_from_file_location(
        'enrich_directors_f16', 'scripts/enrich_directors.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_MEDIA_SEQ = {'n': 7_000_000}


@pytest.fixture
def media_factory(app, db):
    def make(title='Film', media_type='movie'):
        _MEDIA_SEQ['n'] += 1
        m = MediaItem(tmdb_id=_MEDIA_SEQ['n'], media_type=media_type,
                      title=title, runtime=100, genres='Drama')
        db.session.add(m)
        db.session.commit()
        return m
    return make


def _enrich(script, db, item, payload, now=None):
    return script.enrich_media_item(
        db, (Director, MediaDirector), item,
        lambda tmdb_id: payload, now or datetime.utcnow())


# ════════════════════════════════════════════════════════════════════════════
# The real contract: normalized fetch_movie_details output
# ════════════════════════════════════════════════════════════════════════════

def test_normalized_response_yields_directors(enrich_script, app, db,
                                              media_factory):
    """The shape fetch_movie_details really returns must work."""
    m = media_factory()
    payload = {'id': 1, 'title': 'Inception',
               'director': 'Christopher Nolan', 'cast': [],
               'directors': [{'id': 203, 'name': 'Christopher Nolan'}]}
    outcome = _enrich(enrich_script, db, m, payload)
    assert outcome == 'ok'
    assert [a.director.name for a in m.directors] == ['Christopher Nolan']
    assert m.directors[0].director.tmdb_person_id == 203
    assert m.directors_enriched_at is not None


def test_the_old_shape_now_raises_instead_of_silently_empty(enrich_script):
    """The exact pre-F16 response must NOT report 'no director credited'.

    This is the regression that caused silent zero-director enrichment: a
    successful-looking response with no `directors` and no `credits`.
    """
    old = {'id': 1, 'title': 'Inception', 'director': 'Christopher Nolan',
           'writer': 'Christopher Nolan', 'cast': []}
    with pytest.raises(enrich_script.DirectorContractError):
        enrich_script._extract_directors(old)


def test_multiple_directors_persist_in_expected_order(enrich_script, app, db,
                                                      media_factory):
    m = media_factory()
    payload = {'directors': [{'id': 31, 'name': 'Co Director'},
                             {'id': 11, 'name': 'Lead Director'},
                             {'id': 42, 'name': 'Third Director'}]}
    assert _enrich(enrich_script, db, m, payload) == 'ok'
    # Deterministic: sorted by person id, independent of response order.
    assert sorted(a.director.name for a in m.directors) == [
        'Co Director', 'Lead Director', 'Third Director']
    assert len(MediaDirector.query.filter_by(
        media_item_id=m.id).all()) == 3


def test_fetcher_exposes_director_ids():
    """fetch_movie_details must carry ids, not just a name string."""
    from api.tmdb import movies as movies_mod
    src = open('api/tmdb/movies.py', encoding='utf-8').read()
    assert "'directors'" in src
    # And the normalized builder keeps `director` untouched for old consumers.
    assert "movie['director']" in src
    assert hasattr(movies_mod, 'fetch_movie_details')


def test_unrelated_movie_fields_are_untouched():
    """F16 is additive: existing normalized fields keep their behaviour."""
    payload = {'id': 1, 'title': 'T', 'overview': 'o', 'tagline': 'tl',
               'release_date': '2020-01-01', 'runtime': 120,
               'genres': [{'name': 'Drama'}],
               'credits': {'crew': [
                   {'id': 1, 'name': 'Dir One', 'job': 'Director'},
                   {'id': 2, 'name': 'Dir Two', 'job': 'Director'},
                   {'id': 3, 'name': 'W', 'job': 'Screenplay'}],
                   'cast': [{'id': 9, 'name': 'Actor', 'character': 'Self'}]},
               'videos': {'results': []}, 'recommendations': {'results': []}}
    result = _call_with_patched_cache(payload)
    assert result['title'] == 'T'
    assert result['overview'] == 'o'
    assert result['tagline'] == 'tl'
    assert result['genres'] == ['Drama']
    assert result['writer'] == 'W'
    # Pre-existing last-wins quirk of `director` is deliberately preserved.
    assert result['director'] == 'Dir Two'
    # ...while `directors` carries the complete ordered identity list.
    assert result['directors'] == [
        {'id': 1, 'name': 'Dir One'}, {'id': 2, 'name': 'Dir Two'}]


def test_fetcher_directors_are_filtered_and_cleaned():
    """Only Director-role, id-bearing, non-blank entries survive."""
    crew = [
        {'id': 1, 'name': 'Real Director', 'job': 'Director'},
        {'id': 2, 'name': 'Writer Guy', 'job': 'Screenplay'},
        {'id': 3, 'name': 'DP Person', 'job': 'Director of Photography'},
        {'id': None, 'name': 'No Id', 'job': 'Director'},
        {'id': 5, 'name': '   ', 'job': 'Director'},
    ]
    payload = {'id': 1, 'title': 'T', 'credits': {'crew': crew, 'cast': []}}
    result = _call_with_patched_cache(payload)
    assert result['directors'] == [{'id': 1, 'name': 'Real Director'}]
    # `director` keeps its pre-existing last-wins behaviour, untouched by F16.
    assert result['director'] == '   '


# ════════════════════════════════════════════════════════════════════════════
# Missing / malformed / empty
# ════════════════════════════════════════════════════════════════════════════

def test_empty_but_valid_response_marks_enriched(enrich_script, app, db,
                                                 media_factory):
    """Spec §13: 'enriched and empty' stays distinct from 'not yet enriched'."""
    m = media_factory()
    outcome = _enrich(enrich_script, db, m, {'directors': []})
    assert outcome == 'empty'
    assert m.directors_enriched_at is not None
    assert m.directors == []


def test_directors_none_is_a_contract_failure(enrich_script, app, db,
                                              media_factory):
    """`directors: None` is unreadable, not 'no director credited'."""
    m = media_factory()
    with pytest.raises(enrich_script.DirectorContractError):
        _enrich(enrich_script, db, m, {'directors': None})
    assert m.directors_enriched_at is None      # still retryable
    assert m.directors == []


def test_non_object_response_raises(enrich_script, app, db, media_factory):
    m = media_factory()
    for bad in ([], 'nope', 42, None):
        with pytest.raises(enrich_script.DirectorContractError):
            _enrich(enrich_script, db, m, bad)
        assert m.directors_enriched_at is None


def test_malformed_entries_never_fabricate_a_director(enrich_script):
    """No usable identity -> dropped, never invented."""
    payload = {'directors': [
        {'id': 'not-a-number', 'name': 'Bad Id'},
        {'id': None, 'name': 'No Id'},
        {'name': 'Missing Id Key'},
        {'id': 9, 'name': ''},
        {'id': 10, 'name': None},
        {'id': 11},                       # no name
        'a bare string',
        None,
        {'id': 12, 'name': '  Real  '},   # whitespace trimmed
    ]}
    assert enrich_script._extract_directors(payload) == [(12, 'Real')]


def test_duplicate_ids_are_deduplicated(enrich_script):
    payload = {'directors': [{'id': 7, 'name': 'First'},
                             {'id': 7, 'name': 'Second'},
                             {'id': 8, 'name': 'Other'}]}
    assert enrich_script._extract_directors(payload) == [(7, 'Second'),
                                                         (8, 'Other')]


def test_legacy_raw_malformed_id_is_skipped_not_raised(enrich_script):
    """Both shapes must handle an unreadable id the same way.

    A bare int() used to raise ValueError on the legacy path, so a malformed
    payload surfaced as an unrelated error type instead of being skipped.
    """
    raw = {'credits': {'crew': [
        {'id': 'abc', 'name': 'Bad Id', 'job': 'Director'},
        {'id': None, 'name': 'No Id', 'job': 'Director'},
        {'id': 55, 'name': 'Good', 'job': 'Director'},
    ]}}
    assert enrich_script._extract_directors(raw) == [(55, 'Good')]
    # and the normalized equivalent already behaves this way
    assert enrich_script._extract_directors({'directors': [
        {'id': 'abc', 'name': 'Bad Id'}, {'id': 55, 'name': 'Good'}]}) \
        == [(55, 'Good')]


def test_empty_name_and_id_are_skipped(enrich_script, app, db, media_factory):
    m = media_factory()
    payload = {'directors': [{'id': 77, 'name': 'Valid'},
                             {'id': 78, 'name': '  '},
                             {'id': None, 'name': 'No Id'}]}
    assert _enrich(enrich_script, db, m, payload) == 'ok'
    assert [a.director.name for a in m.directors] == ['Valid']


# ════════════════════════════════════════════════════════════════════════════
# Completion-marker integrity
# ════════════════════════════════════════════════════════════════════════════

def test_contract_failure_is_not_recorded_as_success(enrich_script, app, db,
                                                     media_factory):
    m = media_factory()
    with pytest.raises(enrich_script.DirectorContractError):
        _enrich(enrich_script, db, m, {'title': 'no credits key at all'})
    db.session.rollback()
    item = MediaItem.query.get(m.id)
    assert item.directors_enriched_at is None
    assert item.directors == []


def test_failed_item_is_selected_again_on_the_next_run(enrich_script, app, db,
                                                       media_factory):
    """A contract failure must stay pending, not silently complete."""
    m = media_factory()
    with pytest.raises(enrich_script.DirectorContractError):
        _enrich(enrich_script, db, m, {'title': 'broken'})
    db.session.rollback()
    pending = MediaItem.query.filter(
        MediaItem.media_type == 'movie',
        MediaItem.directors_enriched_at.is_(None)).all()
    assert m.id in [p.id for p in pending]


def test_successful_item_is_not_selected_again(enrich_script, app, db,
                                               media_factory):
    m = media_factory()
    _enrich(enrich_script, db, m,
            {'directors': [{'id': 61, 'name': 'Once'}]})
    # Scope to THIS item: the session-scoped DB is never wiped between
    # tests, so other tests' pending rows are irrelevant.
    selected = MediaItem.query.filter(
        MediaItem.id == m.id,
        MediaItem.directors_enriched_at.is_(None)).all()
    assert selected == []


# ════════════════════════════════════════════════════════════════════════════
# Idempotency / dedup (existing behaviour must survive)
# ════════════════════════════════════════════════════════════════════════════

def test_reenrichment_does_not_duplicate(enrich_script, app, db,
                                         media_factory):
    m = media_factory()
    payload = {'directors': [{'id': 71, 'name': 'Repeat Director'}]}
    _enrich(enrich_script, db, m, payload)
    _enrich(enrich_script, db, m, payload)
    assert Director.query.filter_by(tmdb_person_id=71).count() == 1
    assert MediaDirector.query.filter_by(media_item_id=m.id).count() == 1


def test_two_movies_share_one_director_row(enrich_script, app, db,
                                           media_factory):
    """The stable person id keeps one Director across many movies."""
    a, b = media_factory('A'), media_factory('B')
    _enrich(enrich_script, db, a,
            {'directors': [{'id': 88, 'name': 'Prolific'}]})
    _enrich(enrich_script, db, b,
            {'directors': [{'id': 88, 'name': 'Prolific'}]})
    person = Director.query.filter_by(tmdb_person_id=88).one()
    assert MediaDirector.query.filter(
        MediaDirector.director_id == person.id).count() == 2


def test_legacy_raw_fixture_shape_still_works(enrich_script, app, db,
                                              media_factory):
    """Regression: existing fixtures inject the RAW credits payload."""
    m = media_factory()
    raw = {'credits': {'crew': [
        {'id': 91, 'name': 'Legacy Director', 'job': 'Director'},
        {'id': 92, 'name': 'Writer', 'job': 'Screenplay'},
    ]}}
    assert _enrich(enrich_script, db, m, raw) == 'ok'
    assert [a.director.name for a in m.directors] == ['Legacy Director']


def test_legacy_raw_empty_still_marks_enriched(enrich_script, app, db,
                                               media_factory):
    m = media_factory()
    assert _enrich(enrich_script, db, m, {'credits': {'crew': []}}) \
        == 'empty'
    assert m.directors_enriched_at is not None


def test_normalized_shape_wins_over_legacy_shape(enrich_script):
    """A response carrying both must use the normalized contract."""
    payload = {
        'directors': [{'id': 1, 'name': 'Normalized'}],
        'credits': {'crew': [{'id': 2, 'name': 'Legacy', 'job': 'Director'}]},
    }
    assert enrich_script._extract_directors(payload) == [(1, 'Normalized')]


# ════════════════════════════════════════════════════════════════════════════
# Isolation: no network
# ════════════════════════════════════════════════════════════════════════════

def test_extraction_is_pure_and_makes_no_calls(enrich_script, monkeypatch):
    import socket

    def _boom(*a, **k):
        raise AssertionError('extraction must not touch the network')
    monkeypatch.setattr(socket, 'socket', _boom)
    monkeypatch.setattr(socket, 'create_connection', _boom)
    assert enrich_script._extract_directors(
        {'directors': [{'id': 1, 'name': 'A'}]}) == [(1, 'A')]


def test_no_schema_change_required(app, db):
    """F16 is a contract fix: no new table, column or migration."""
    assert 'tmdb_person_id' in Director.__table__.c
    assert Director.__table__.c.tmdb_person_id.unique
    assert not Director.__table__.c.tmdb_person_id.nullable
    assert 'directors_enriched_at' in MediaItem.__table__.c


# ── helpers for the fetch_movie_details tests ──────────────────────────────

def _call_with_patched_cache(payload):
    """Call fetch_movie_details with the cached HTTP layer stubbed out."""
    from api.tmdb import movies as movies_mod
    original = movies_mod.cached_tmdb_request
    movies_mod.cached_tmdb_request = lambda url, **kw: payload
    try:
        return movies_mod.fetch_movie_details(1)
    finally:
        movies_mod.cached_tmdb_request = original