"""Director affinity data capture (Feature #6, Phase 10).

Covers the persistence layer (Director / MediaDirector /
MediaItem.directors_enriched_at), the idempotent migration, and the
bounded enrichment script — the ONLY path allowed to contact TMDb for
director data. The TasteProfile/For You sides are covered in their own
suites (test_taste_profile_service.py, test_for_you.py).
"""
import importlib.util
import subprocess
import sys
import uuid
from datetime import datetime

import pytest

from models import MediaItem, TasteProfile, db
from models.director import Director, MediaDirector


# ── Fixtures / helpers ───────────────────────────────────────────────────────

_TMDB_COUNTER = {'n': 950000}


def _uid(prefix):
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


@pytest.fixture
def user(app, db):
    from models import User
    u = User(username=_uid('director'), email=f"{_uid('director')}@x.io")
    u.set_password('password123')
    db.session.add(u)
    db.session.commit()
    return u


@pytest.fixture
def media_factory(app, db):
    def _make(media_type='movie', title='Dune', genres='Drama, Crime',
              runtime=120):
        _TMDB_COUNTER['n'] += 1
        m = MediaItem(tmdb_id=_TMDB_COUNTER['n'], media_type=media_type,
                      title=title, genres=genres, runtime=runtime)
        db.session.add(m)
        db.session.commit()
        return m
    return _make


@pytest.fixture
def director_factory(app, db):
    def _make(person_id, name='Denis Villeneuve'):
        d = Director(tmdb_person_id=person_id, name=name, source='tmdb')
        db.session.add(d)
        db.session.commit()
        return d
    return _make


@pytest.fixture
def enrich_script():
    spec = importlib.util.spec_from_file_location(
        'enrich_directors_test', 'scripts/enrich_directors.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ════════════════════════════════════════════════════════════════════════════
# Storage / model (spec §21: 1–9)
# ════════════════════════════════════════════════════════════════════════════

def test_models_registered_in_metadata(app, db):
    assert 'director' in db.metadata.tables
    assert 'media_director' in db.metadata.tables


def test_exact_columns(app, db):
    assert [c.name for c in Director.__table__.columns] == [
        'id', 'tmdb_person_id', 'name', 'source', 'created_at', 'updated_at']
    assert [c.name for c in MediaDirector.__table__.columns] == [
        'id', 'media_item_id', 'director_id', 'created_at']
    assert 'directors_enriched_at' in [
        c.name for c in MediaItem.__table__.columns]


def test_stable_person_id_and_defaults(app, db, director_factory):
    d = director_factory(990001, 'Christopher Nolan')
    assert d.tmdb_person_id == 990001
    assert d.source == 'tmdb'
    assert d.created_at is not None
    d2 = Director.query.filter_by(tmdb_person_id=990001).one()
    assert d2.id == d.id


def test_display_name_update_does_not_fork_person(app, db, director_factory):
    d = director_factory(990002, 'Old Name')
    d.name = 'New Name'
    db.session.commit()
    assert Director.query.filter_by(tmdb_person_id=990002).count() == 1
    assert Director.query.filter_by(tmdb_person_id=990002).one().name \
        == 'New Name'


def test_media_director_association_and_uniqueness(
        app, db, user, media_factory, director_factory):
    m = media_factory()
    d1 = director_factory(990003, 'Director One')
    d2 = director_factory(990004, 'Director Two')
    db.session.add(MediaDirector(media_item_id=m.id, director_id=d1.id))
    db.session.add(MediaDirector(media_item_id=m.id, director_id=d2.id))
    db.session.commit()
    assert m.directors_enriched_at is None  # capture status is separate
    with pytest.raises(Exception):
        db.session.add(MediaDirector(media_item_id=m.id, director_id=d1.id))
        db.session.flush()


def test_cascade_on_media_delete(app, db, user, media_factory,
                                 director_factory):
    m = media_factory()
    d = director_factory(990005)
    db.session.add(MediaDirector(media_item_id=m.id, director_id=d.id))
    db.session.commit()
    db.session.delete(m)
    db.session.commit()
    assert MediaDirector.query.filter_by(director_id=d.id).count() == 0


def test_cascade_on_director_delete(app, db, user, media_factory,
                                    director_factory):
    m = media_factory()
    d = director_factory(990006)
    db.session.add(MediaDirector(media_item_id=m.id, director_id=d.id))
    db.session.commit()
    db.session.delete(d)
    db.session.commit()
    assert MediaDirector.query.filter_by(media_item_id=m.id).count() == 0


def test_no_unrelated_schema_changes(app, db):
    from sqlalchemy import inspect
    tables = set(inspect(db.engine).get_table_names())
    assert 'user_taste_profile' not in tables
    assert 'user_similarity' not in tables
    assert {'user', 'media_item', 'taste_profile'} <= tables


# ════════════════════════════════════════════════════════════════════════════
# Migration (spec §21: 10–12) — subprocess, like the other migration tests
# ════════════════════════════════════════════════════════════════════════════

def _run_migration(db_file):
    import os
    env = dict(os.environ)
    env.update({
        'DATABASE_URL': f'sqlite:///{db_file}',
        'SECRET_KEY': 'test-secret-key-for-tests',
        'TMDB_API_KEY': 'test-key',
        'SKIP_SCHEMA_GUARD': '1',
    })
    env.pop('RATELIMIT_STORAGE_URI', None)
    return subprocess.run(
        [sys.executable, 'migrates/migrate_director_capture.py'],
        capture_output=True, text=True, env=env, timeout=120)


def test_migration_creates_alter_idempotent(tmp_path):
    import sqlite3
    db_file = tmp_path / 'legacy.db'
    conn = sqlite3.connect(db_file)
    # Legacy-shaped DB: media_item WITHOUT directors_enriched_at, plus data
    # and the legacy taste tables that must remain untouched.
    conn.executescript("""
        CREATE TABLE media_item (
            id INTEGER PRIMARY KEY, tmdb_id INTEGER, media_type TEXT,
            title TEXT, release_date DATE, poster_path TEXT, genres TEXT,
            overview TEXT, rating FLOAT, runtime INTEGER);
        INSERT INTO media_item (tmdb_id, media_type, title)
            VALUES (238, 'movie', 'The Godfather');
        CREATE TABLE user_taste_profile (id INTEGER PRIMARY KEY);
        CREATE TABLE user_similarity (id INTEGER PRIMARY KEY);
    """)
    conn.commit()
    conn.close()

    run1 = _run_migration(db_file)
    assert run1.returncode == 0, run1.stdout + run1.stderr
    assert 'Added media_item.directors_enriched_at' in run1.stdout

    # RUN 2 — idempotent no-op.
    run2 = _run_migration(db_file)
    assert run2.returncode == 0, run2.stdout + run2.stderr
    assert 'already exists' in run2.stdout

    conn = sqlite3.connect(db_file)
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    cols = {r[1] for r in conn.execute('PRAGMA table_info(media_item)')}
    rows = conn.execute('SELECT tmdb_id, title FROM media_item').fetchall()
    conn.close()
    assert {'director', 'media_director'} <= tables
    assert 'directors_enriched_at' in cols
    assert rows == [(238, 'The Godfather')]  # data untouched
    assert 'user_taste_profile' in tables and 'user_similarity' in tables


# ════════════════════════════════════════════════════════════════════════════
# Enrichment script (spec §22: 1–13) — mocked TMDb only
# ════════════════════════════════════════════════════════════════════════════

def _credits(crew):
    return {'credits': {'crew': crew}}


def _crew(person_id, name, job):
    return {'id': person_id, 'name': name, 'job': job}


def _patch_fetch(monkeypatch, responses):
    """responses: {tmdb_id: payload | Exception}. Calls are counted."""
    calls = []

    def fake_fetch(tmdb_id, *a, **kw):
        calls.append(tmdb_id)
        payload = responses.get(tmdb_id)
        if isinstance(payload, Exception):
            raise payload
        return payload

    monkeypatch.setattr('api.tmdb.movies.fetch_movie_details', fake_fetch)
    return calls


def _write_enriched_marker(item):
    item.directors_enriched_at = datetime.utcnow()
    db.session.commit()


def _mark_all_enriched():
    """Isolate run-scoped tests: earlier tests leave pending items in the
    shared DB — mark them enriched so run() only sees this test's items."""
    MediaItem.query.filter_by(media_type='movie').filter(
        MediaItem.directors_enriched_at.is_(None)).update(
        {'directors_enriched_at': datetime.utcnow()})
    db.session.commit()


def test_enrich_persists_directors_and_marker(
        app, db, enrich_script, media_factory, monkeypatch):
    m = media_factory()
    calls = _patch_fetch(monkeypatch, {
        m.tmdb_id: _credits([_crew(990100, 'Denis Villeneuve', 'Director'),
                             _crew(990101, 'Someone', 'Director of Photography'),
                             _crew(990102, 'Writer Guy', 'Screenplay')]),
    })
    with app.app_context():
        assert enrich_script.enrich_media_item(
            db, (Director, MediaDirector), m,
            __import__('api.tmdb.movies', fromlist=['fetch_movie_details'])
            .fetch_movie_details, datetime.utcnow()) == 'ok'
    assert calls == [m.tmdb_id]
    names = sorted(a.director.name for a in m.directors)
    assert names == ['Denis Villeneuve']  # only the Director job persisted
    assert m.directors_enriched_at is not None
    assert Director.query.filter_by(tmdb_person_id=990101).count() == 0


def test_enrich_reenrich_does_not_duplicate_rows(
        app, db, enrich_script, media_factory, monkeypatch):
    m = media_factory()
    calls = _patch_fetch(monkeypatch, {
        m.tmdb_id: _credits([_crew(990110, 'Director A', 'Director')]),
    })
    with app.app_context():
        from api.tmdb.movies import fetch_movie_details as fetch
        enrich_script.enrich_media_item(
            db, (Director, MediaDirector), m, fetch, datetime.utcnow())
        # Re-enriching the same item (helper level) must never duplicate
        # the person row or the (media, director) association. Selection-
        # level idempotency (already-enriched items are never re-fetched)
        # is proven in test_enrich_selection_*.
        enrich_script.enrich_media_item(
            db, (Director, MediaDirector), m, fetch, datetime.utcnow())
    assert calls == [m.tmdb_id, m.tmdb_id]  # helper-level: both fetched
    assert MediaDirector.query.filter_by(media_item_id=m.id).count() == 1
    assert Director.query.filter_by(tmdb_person_id=990110).count() == 1


def test_enrich_selection_skips_enriched_and_tv_is_bounded(
        app, db, enrich_script, media_factory, monkeypatch):
    fresh1, fresh2 = media_factory(), media_factory()
    enriched = media_factory()
    tv = media_factory(media_type='tv')
    _write_enriched_marker(enriched)
    calls = _patch_fetch(monkeypatch, {})
    with app.app_context():
        batch = enrich_script._select_pending(db, 100)
    assert calls == []  # selection itself never touches TMDb
    ids = {m.id for m in batch}
    assert {fresh1.id, fresh2.id} <= ids
    assert enriched.id not in ids
    assert tv.id not in ids  # TV deliberately excluded (spec §10)
    assert enrich_script.MAX_MEDIA_ITEMS == 100


def test_enrich_selection_respects_batch_cap(
        app, db, enrich_script, media_factory):
    for _ in range(6):
        media_factory()
    with app.app_context():
        batch = enrich_script._select_pending(db, 4)
    assert len(batch) == 4  # stable order (MediaItem.id), bounded


def test_enrich_multiple_directors_and_name_update(
        app, db, enrich_script, media_factory):
    m = media_factory()
    existing = Director(tmdb_person_id=990130, name='Old Name', source='tmdb')
    db.session.add(existing)
    db.session.commit()
    with app.app_context():
        enrich_script.enrich_media_item(
            db, (Director, MediaDirector), m,
            lambda tmdb_id: _credits([
                _crew(990130, 'New Name', 'Director'),
                _crew(990131, 'Co Director', 'Director')]),
            datetime.utcnow())
    assert sorted(a.director.name for a in m.directors) == [
        'Co Director', 'New Name']
    assert Director.query.filter_by(tmdb_person_id=990130).count() == 1


def test_enrich_empty_result_marks_enriched(
        app, db, enrich_script, media_factory):
    m = media_factory()
    with app.app_context():
        outcome = enrich_script.enrich_media_item(
            db, (Director, MediaDirector), m,
            lambda tmdb_id: _credits([]), datetime.utcnow())
    assert outcome == 'empty'
    assert m.directors_enriched_at is not None
    assert m.directors == []  # enriched-and-empty ≠ unenriched


def test_enrich_failure_is_isolated_and_retryable(
        app, db, enrich_script, media_factory, monkeypatch):
    _mark_all_enriched()
    bad, good = media_factory(), media_factory()
    calls = _patch_fetch(monkeypatch, {
        bad.tmdb_id: RuntimeError('tmdb down'),
        good.tmdb_id: _credits([_crew(990140, 'Good Director', 'Director')]),
    })
    with app.app_context():
        assert enrich_script.run() == 1  # failures → non-zero
    assert calls == [bad.tmdb_id, good.tmdb_id]  # bad didn't stop the batch
    assert good.directors != []
    assert bad.directors == []
    assert bad.directors_enriched_at is None  # NOT marked — retryable
    with app.app_context():
        pending = enrich_script._select_pending(db, 100)
    assert bad.id in {m.id for m in pending}  # selected again next run
    assert good.id not in {m.id for m in pending}


def test_enrich_run_zero_exit_all_success(
        app, db, enrich_script, media_factory, monkeypatch):
    _mark_all_enriched()
    m = media_factory()
    _patch_fetch(monkeypatch, {
        m.tmdb_id: _credits([_crew(990150, 'Director Z', 'Director')])})
    with app.app_context():
        assert enrich_script.run() == 0


def test_enrich_run_no_network_outside_tmdb_path(
        app, db, enrich_script, media_factory, monkeypatch):
    """Enrichment's network access is exclusively fetch_movie_details —
    any other socket creation during a run is forbidden."""
    _mark_all_enriched()
    m = media_factory()
    _patch_fetch(monkeypatch, {
        m.tmdb_id: _credits([_crew(990160, 'Director N', 'Director')])})

    import socket as socket_mod
    created = []

    class _Guard(socket_mod.socket):
        def __init__(self, *a, **kw):
            created.append(1)
            raise AssertionError('direct socket use outside fetch path')

    monkeypatch.setattr(socket_mod, 'socket', _Guard)
    with app.app_context():
        assert enrich_script.run() == 0
    assert created == []  # fetch was monkeypatched; nothing else dialed out


def test_enrichment_script_is_the_only_tmdb_touchpoint_for_directors():
    """Source guard: taste_profile + for_you contain no director network
    path (no details-with-credits fetch, no person endpoint, no
    append_to_response credits); the enrichment script is the only file
    allowed to fetch them."""
    tp = open('api/taste_profile.py').read()
    fy = open('api/for_you.py').read()
    for src in (tp, fy):
        assert 'fetch_movie_details' not in src
        assert 'append_to_response' not in src
        assert "'person/" not in src and '"person/' not in src


# ════════════════════════════════════════════════════════════════════════════
# TasteProfile integration (spec §23) — core assertions; the full signal-
# matrix lives in test_taste_profile_service.py, which this complements.
# ════════════════════════════════════════════════════════════════════════════

def test_profile_director_roundtrip_and_persistence(
        app, db, user, media_factory, director_factory):
    m = media_factory()
    d = director_factory(990170, 'Persisted Director')
    db.session.add(MediaDirector(media_item_id=m.id, director_id=d.id))
    # One real positive user signal (MediaLike.media_id is a TMDb id) so
    # the evidence event exists for the director dimension to ride on.
    from models import MediaLike
    db.session.add(MediaLike(user_id=user.id, media_id=m.tmdb_id,
                             media_type='movie'))
    db.session.commit()
    import api.taste_profile as tp
    tp.compute_profile(user.id)
    fetched = TasteProfile.query.filter_by(user_id=user.id).one()
    assert list(fetched.director_affinity) == ['Persisted Director']
    assert fetched.director_affinity['Persisted Director'] > 0
    # The director dimension reuses the SAME evidence event: one signal,
    # one distinct title — the extra dimension must not inflate counters.
    assert fetched.signal_count == 1
    assert fetched.distinct_title_count == 1


def test_profile_no_director_evidence_stays_empty(
        app, db, user, media_factory):
    media_factory()  # exists but unenriched → no local director rows
    import api.taste_profile as tp
    tp.compute_profile(user.id)
    fetched = TasteProfile.query.filter_by(user_id=user.id).one()
    assert fetched.director_affinity == {}
