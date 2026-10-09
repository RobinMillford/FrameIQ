"""Cast persistence (Feature F9).

Covers the persistence layer (Person / MediaCast /
MediaItem.cast_enriched_at), the additive versioned migration, and the bounded
resumable enrichment script — the ONLY path allowed to contact TMDb for cast
data. Also covers the two consumers that were previously blocked on the
missing cast evidence: api/statistics.py's ``actors`` series (hard-coded empty
before F9) and api/taste_profile.py's new actor-affinity dimension.

Modelled on tests/test_director_capture.py, and deliberately asserts the same
invariants, because cast is the same durable shape applied to a richer credit
payload (character + billing order) and to TV as well as movies.
"""
import importlib.util
import os
import subprocess
import sys
import uuid
from datetime import datetime

import pytest
from requests.exceptions import ConnectionError

from models import MediaItem, TasteProfile, db
from models.cast import MediaCast, Person


REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ── Fixtures / helpers ───────────────────────────────────────────────────────

_TMDB_COUNTER = {'n': 970000}


def _uid(prefix):
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


@pytest.fixture
def user(app, db):
    from models import User
    u = User(username=_uid('cast'), email=f"{_uid('cast')}@x.io")
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
def person_factory(app, db):
    def _make(person_id, name='Zendaya', profile_url=None):
        p = Person(tmdb_person_id=person_id, name=name, source='tmdb',
                   profile_url=profile_url)
        db.session.add(p)
        db.session.commit()
        return p
    return _make


def _enrich_module():
    """Load scripts/enrich_cast.py as a module (no app import happens)."""
    spec = importlib.util.spec_from_file_location(
        'enrich_cast_test', os.path.join(REPO, 'scripts', 'enrich_cast.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def enrich_script():
    return _enrich_module()


def _details(cast):
    """The SHAPE api.tmdb.movies.fetch_movie_details actually returns."""
    return {'id': 1, 'title': 'T', 'cast': cast}


def _member(person_id, name, character=None, profile_url=None):
    return {'id': person_id, 'name': name, 'character': character,
            'profile_path': profile_url}


def _patch_fetch(monkeypatch, responses):
    """responses: {tmdb_id: payload | Exception}. Calls are counted per fetcher."""
    movie_calls, tv_calls = [], []

    def fake_movie(tmdb_id, *a, **kw):
        movie_calls.append(tmdb_id)
        payload = responses.get(tmdb_id)
        if isinstance(payload, Exception):
            raise payload
        return payload

    def fake_tv(tmdb_id, *a, **kw):
        tv_calls.append(tmdb_id)
        payload = responses.get(tmdb_id)
        if isinstance(payload, Exception):
            raise payload
        return payload

    monkeypatch.setattr('api.tmdb.movies.fetch_movie_details', fake_movie)
    monkeypatch.setattr('api.tmdb.tv.fetch_tv_show_details', fake_tv)
    return movie_calls, tv_calls


def _write_marker(item):
    item.cast_enriched_at = datetime.utcnow()
    db.session.commit()


def _mark_all_enriched():
    """Isolate run-scoped tests: earlier tests leave pending items in the shared
    DB, so mark them all so run() only ever sees this test's titles."""
    MediaItem.query.filter(MediaItem.cast_enriched_at.is_(None)).update(
        {'cast_enriched_at': datetime.utcnow()}, synchronize_session=False)
    db.session.commit()


# ════════════════════════════════════════════════════════════════════════════
# Storage / model
# ════════════════════════════════════════════════════════════════════════════

def test_models_registered_in_metadata(app, db):
    assert 'person' in db.metadata.tables
    assert 'media_cast' in db.metadata.tables


def test_exact_columns(app, db):
    assert [c.name for c in Person.__table__.columns] == [
        'id', 'tmdb_person_id', 'name', 'profile_url', 'source',
        'created_at', 'updated_at']
    assert [c.name for c in MediaCast.__table__.columns] == [
        'id', 'media_item_id', 'person_id', 'character', 'credit_order',
        'created_at', 'updated_at']
    assert 'cast_enriched_at' in [
        c.name for c in MediaItem.__table__.columns]


def test_cast_marker_is_separate_from_the_director_marker(app, db):
    """TV has cast but will never have a director (models/director.py), so the
    two markers cannot be the same column."""
    columns = {c.name for c in MediaItem.__table__.columns}
    assert 'cast_enriched_at' in columns
    assert 'directors_enriched_at' in columns
    assert 'cast_enriched_at' != 'directors_enriched_at'


def test_stable_person_id_and_defaults(app, db, person_factory):
    p = person_factory(970001, 'Christopher Nolan')
    assert p.tmdb_person_id == 970001
    assert p.source == 'tmdb'
    assert p.created_at is not None
    assert Person.query.filter_by(tmdb_person_id=970001).one().id == p.id


def test_display_name_update_does_not_fork_person(app, db, person_factory):
    p = person_factory(970002, 'Old Name')
    p.name = 'New Name'
    db.session.commit()
    assert Person.query.filter_by(tmdb_person_id=970002).count() == 1
    assert Person.query.filter_by(tmdb_person_id=970002).one().name == 'New Name'


def test_unique_pair_constraint_rejects_a_duplicate_association(
        app, db, media_factory, person_factory):
    m = media_factory()
    p = person_factory(970003, 'Lead')
    db.session.add(MediaCast(media_item_id=m.id, person_id=p.id,
                             credit_order=0))
    db.session.commit()
    with pytest.raises(Exception):
        db.session.add(MediaCast(media_item_id=m.id, person_id=p.id,
                                 credit_order=9))
        db.session.flush()


def test_relationship_is_ordered_by_credit_order(app, db, media_factory,
                                                 person_factory):
    m = media_factory()
    a = person_factory(970010, 'Billed Third')
    b = person_factory(970011, 'Billed First')
    c = person_factory(970012, 'Billed Second')
    db.session.add_all([
        MediaCast(media_item_id=m.id, person_id=a.id, credit_order=2),
        MediaCast(media_item_id=m.id, person_id=b.id, credit_order=0),
        MediaCast(media_item_id=m.id, person_id=c.id, credit_order=1),
    ])
    db.session.commit()
    db.session.expire_all()
    assert [link.person.name for link in m.cast_members] == [
        'Billed First', 'Billed Second', 'Billed Third']


def test_cascade_on_media_delete(app, db, media_factory, person_factory):
    """media_cast rows die with their title; the Person survives."""
    m = media_factory()
    p = person_factory(970004)
    db.session.add(MediaCast(media_item_id=m.id, person_id=p.id))
    db.session.commit()
    db.session.delete(m)
    db.session.commit()
    assert MediaCast.query.filter_by(person_id=p.id).count() == 0
    assert Person.query.filter_by(tmdb_person_id=970004).count() == 1


def test_cascade_on_person_delete(app, db, media_factory, person_factory):
    m = media_factory()
    p = person_factory(970005)
    db.session.add(MediaCast(media_item_id=m.id, person_id=p.id))
    db.session.commit()
    db.session.delete(p)
    db.session.commit()
    assert MediaCast.query.filter_by(media_item_id=m.id).count() == 0


def test_no_unrelated_schema_changes(app, db):
    from sqlalchemy import inspect
    tables = set(inspect(db.engine).get_table_names())
    # The legacy tables stay retired — F9 creates nothing that resurrects them.
    assert 'user_similarity' not in tables
    assert 'user_wishlist' not in tables
    assert {'user', 'media_item', 'person', 'media_cast'} <= tables


def test_cast_tables_are_absent_from_the_historical_convergence_allow_list(
        app, db):
    """F9 must NOT widen the bounded historical repair set.

    Those tables are created by the runner and recorded in the ledger; letting
    convergence create them would be an unledgered creation path — the exact
    mistake that produced schema_migrations in F8.
    """
    import importlib.util as _u
    spec = _u.spec_from_file_location(
        'conv_f9', os.path.join(REPO, 'migrates',
                                'migrate_schema_convergence.py'))
    # Importing the script runs app-level setup, so read the literal instead.
    source = open(os.path.join(REPO, 'migrates',
                               'migrate_schema_convergence.py')).read()
    body = source.split('EXPECTED_REPAIR_SET = frozenset({', 1)[1]
    body = body.split('})', 1)[0]
    declared = {line.strip().strip("',") for line in body.split('\n')
                if line.strip()}
    assert 'person' not in declared
    assert 'media_cast' not in declared
    assert len(declared) == 11, sorted(declared)
    assert spec is not None


# ════════════════════════════════════════════════════════════════════════════
# Migration: additive, ordering, checksums, 0002 independence
# ════════════════════════════════════════════════════════════════════════════

def _pre_f9_db(tmp_path, name, taste_profile_rows=0):
    """A database shaped exactly like production before F9.

    The full declared schema, then F9's objects removed: person, media_cast,
    MediaItem.cast_enriched_at and TasteProfile.actor_affinity_json. That is the
    real upgrade target -- not an empty database, and not one that already has
    the new tables.

    ``taste_profile_rows`` seeds pre-existing TasteProfile rows. It matters:
    ``actor_affinity_json`` is added by ALTER TABLE to an already-populated
    table in production, and a NOT NULL column with no server-side default is
    refused outright. Seeding with 0 would hide that.
    """
    import sqlite3

    from sqlalchemy import create_engine
    from models.base import db

    path = tmp_path / name
    engine = create_engine('sqlite:///%s' % path)
    db.metadata.create_all(bind=engine)
    engine.dispose()

    conn = sqlite3.connect(str(path))
    conn.execute('DROP TABLE media_cast')
    conn.execute('DROP TABLE person')
    conn.execute('ALTER TABLE media_item DROP COLUMN cast_enriched_at')
    conn.execute('ALTER TABLE taste_profile DROP COLUMN actor_affinity_json')
    for index in range(taste_profile_rows):
        conn.execute(
            'INSERT INTO taste_profile '
            '(id, user_id, genre_weights_json, decade_weights_json, '
            ' director_affinity_json, runtime_pref_json, '
            ' media_type_pref_json, mood_tags_json, confidence, '
            ' signal_count, distinct_title_count, profile_version, '
            ' created_at, updated_at) '
            'VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, '
            "        CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
            (index + 1, 100 + index,
             '{"Drama": 0.5}', '{"2010s": 0.5}', '{"Old Director": 1.0}',
             '{"p25": 90, "p75": 120, "sample_count": 3}',
             '{"movie": 0.8, "tv": 0.2}', 0.4, 7, 6, 1))
    conn.commit()
    conn.close()
    return path


def _run_cli(db_path, *args):
    env = dict(os.environ)
    env.update({
        'DATABASE_URL': 'sqlite:///%s' % db_path,
        'SECRET_KEY': 'test-secret-key-for-tests',
        'TMDB_API_KEY': 'test-key',
        'SKIP_SCHEMA_GUARD': '1',
    })
    env.pop('RATELIMIT_STORAGE_URI', None)
    return subprocess.run([sys.executable, 'scripts/migrate.py', *args],
                          capture_output=True, text=True, env=env, cwd=REPO,
                          timeout=180)


def _migration_module():
    sys.path.insert(0, os.path.join(REPO, 'migrates'))
    import migrations_0003_cast_persistence as module
    return module


def test_registered_and_ordered_after_0002_in_the_registry(app, db):
    from migrates import registry
    assert registry.validate() == []
    versions = [s.version for s in registry.ordered_migrations()]
    assert versions == ['0001_canonical_watched_reconcile',
                        '0002_remove_legacy_wishlist',
                        '0003_cast_persistence']
    spec = registry.by_version()['0003_cast_persistence']
    assert spec.depends_on == ('0001_canonical_watched_reconcile',)


def test_depends_on_0001_and_not_on_the_deferred_destructive_migration(app, db):
    """The load-bearing design decision.

    Depending on 0002 would make every additive F9 change unreachable while the
    legacy DROP is blocked, because a forward dependency is refused outright.
    """
    from migrates import registry
    spec = registry.by_version()['0003_cast_persistence']
    assert '0001_canonical_watched_reconcile' in spec.depends_on
    assert '0002_remove_legacy_wishlist' not in spec.depends_on


def test_declared_non_destructive_and_owns_its_new_names(app, db):
    module = _migration_module()
    assert module.DESTRUCTIVE is False
    assert module.CREATES_TABLES == ('person', 'media_cast')
    # actor_affinity_json ships in the SAME migration: api/taste_profile.py
    # writes that column, so a database given only the tables would boot into
    # a schema-guard failure.
    assert module.ADDS_COLUMNS == ('media_item.cast_enriched_at',
                                   'taste_profile.actor_affinity_json')


def test_ordinary_upgrade_applies_f9_while_0002_stays_deferred(tmp_path):
    """End-to-end through the real runner and the real CLI.

    This is the ordering evidence: an ordinary ``upgrade`` applies 0001 then
    0003, DEFERS 0002, and records exactly two rows.
    """
    import sqlite3

    db_path = _pre_f9_db(tmp_path, 'f9.db')
    result = _run_cli(db_path, 'upgrade')
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'DEFERRED' in result.stdout
    assert '0002_remove_legacy_wishlist' in result.stdout
    assert '+ 0003_cast_persistence' in result.stdout
    assert '0002_remove_legacy_wishlist' not in result.stdout.split(
        'Applied 2 migration(s)')[1]

    conn = sqlite3.connect(str(db_path))
    versions = [r[0] for r in conn.execute(
        'SELECT version FROM schema_migrations ORDER BY version')]
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    columns = {r[1] for r in conn.execute('PRAGMA table_info(media_item)')}
    profile_columns = {r[1] for r in conn.execute(
        'PRAGMA table_info(taste_profile)')}
    conn.close()

    assert versions == ['0001_canonical_watched_reconcile',
                        '0003_cast_persistence']
    assert '0002_remove_legacy_wishlist' not in versions
    assert {'person', 'media_cast'} <= tables
    assert 'cast_enriched_at' in columns
    assert 'actor_affinity_json' in profile_columns


def test_migration_writes_no_cast_rows(tmp_path):
    """A migration creates schema; only the offline batch populates it."""
    import sqlite3
    db_path = _pre_f9_db(tmp_path, 'empty.db')
    result = _run_cli(db_path, 'upgrade')
    assert result.returncode == 0, result.stdout + result.stderr

    conn = sqlite3.connect(str(db_path))
    people = conn.execute('SELECT COUNT(*) FROM person').fetchone()[0]
    cast = conn.execute('SELECT COUNT(*) FROM media_cast').fetchone()[0]
    conn.close()
    assert people == 0
    assert cast == 0


def test_migration_is_idempotent_at_the_ddl_level(app, db):
    module = _migration_module()
    from sqlalchemy import create_engine, inspect
    engine = create_engine('sqlite://')
    db.metadata.create_all(bind=engine)
    with engine.connect() as connection:
        with connection.begin():
            assert module.verify(connection) == []
        with connection.begin():
            module.run(connection)          # second, redundant run
        with connection.begin():
            assert module.verify(connection) == []
    assert 'person' in inspect(engine).get_table_names()
    engine.dispose()


def test_checksum_is_recorded_and_stable(app, db):
    sys.path.insert(0, os.path.join(REPO, 'scripts'))
    import migrate as runner
    spec = runner.registry.by_version()['0003_cast_persistence']
    assert runner.checksum_for(spec) == runner.checksum_for(spec)
    assert len(runner.checksum_for(spec)) == 64


def test_verify_reports_a_missing_table(app, db):
    module = _migration_module()
    from sqlalchemy import create_engine, text as sa_text
    engine = create_engine('sqlite://')
    db.metadata.create_all(bind=engine)
    with engine.connect() as connection:
        with connection.begin():
            connection.execute(sa_text('DROP TABLE media_cast'))
        problems = module.verify(connection)
    assert any('media_cast' in p for p in problems)
    engine.dispose()
    assert 'media_cast' in db.metadata.tables  # metadata untouched


def test_name_collision_is_refused_before_any_ddl(app, db):
    """The documented idx_taste_profile_updated incident, as a hard precondition.

    A collision must abort BEFORE DDL, because discovering it via a failing
    CREATE INDEX rolls back every other statement in the transaction.
    """
    module = _migration_module()
    from sqlalchemy import create_engine, inspect, text as sa_text
    engine = create_engine('sqlite://')
    db.metadata.create_all(bind=engine)
    with engine.connect() as connection:
        with connection.begin():
            connection.execute(sa_text('DROP TABLE media_cast'))
            connection.execute(sa_text('DROP TABLE person'))
            # A legacy table already owns one of the names media_cast declares.
            connection.execute(sa_text(
                'CREATE TABLE legacy_owner (id INTEGER PRIMARY KEY)'))
            connection.execute(sa_text(
                'CREATE UNIQUE INDEX uq_media_cast_pair ON legacy_owner(id)'))
        with connection.begin():
            conflicts = module.name_conflicts(connection)
        assert any(name == 'uq_media_cast_pair' and owner == 'legacy_owner'
                   for _kind, name, _wanted, owner in conflicts), conflicts
        with connection.begin():
            with pytest.raises(RuntimeError, match='already owned by'):
                module.run(connection)
        # Nothing was created: the pre-flight refused before any DDL.
        tables = set(inspect(connection).get_table_names())
    assert 'person' not in tables
    assert 'media_cast' not in tables
    engine.dispose()


def test_importing_the_app_creates_no_cast_tables(tmp_path):
    """F9 adds models; it must not add startup DDL."""
    db_path = tmp_path / 'boot.db'
    env = dict(os.environ)
    env.update({
        'DATABASE_URL': f'sqlite:///{db_path}',
        'SECRET_KEY': 'test-secret-key-for-tests',
        'TMDB_API_KEY': 'test-key',
        'SKIP_SCHEMA_GUARD': '1',
    })
    code = (
        "from app import app\n"
        "from models import db\n"
        "from sqlalchemy import inspect\n"
        "with app.app_context():\n"
        "    t = set(inspect(db.engine).get_table_names())\n"
        "print('CAST_TABLES=%d' % len({'person','media_cast'} & t))\n"
    )
    result = subprocess.run([sys.executable, '-c', code], capture_output=True,
                            text=True, env=env, cwd=REPO, timeout=180)
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'CAST_TABLES=0' in result.stdout, result.stdout


# ════════════════════════════════════════════════════════════════════════════
# Enrichment script — parsing, idempotency, resumability, failure handling
# ════════════════════════════════════════════════════════════════════════════

def test_extract_cast_reads_the_fetcher_payload(enrich_script):
    payload = _details([_member(1, 'A', 'Hero'), _member(2, 'B', 'Villain')])
    assert enrich_script._title_level_cast(payload) == [
        (1, 'A', 'Hero', None), (2, 'B', 'Villain', None)]


def test_extract_cast_also_accepts_a_raw_tmdb_body(enrich_script):
    """A raw TMDb body puts the same array under credits.cast.

    Deliberately the TITLE-level credits object — never a season/episode
    payload, which this feature refuses to read.
    """
    raw = {'credits': {'cast': [_member(3, 'C', 'Role'),
                                {'id': 4, 'name': 'D'}]}}
    assert enrich_script._title_level_cast(raw) == [
        (3, 'C', 'Role', None), (4, 'D', None, None)]


def test_extract_cast_captures_order_character_and_profile(enrich_script):
    payload = _details([
        _member(10, 'Lead', 'Protagonist', 'https://img/lead.jpg'),
        _member(11, 'Support', None, None),
    ])
    cast = enrich_script._title_level_cast(payload)
    assert [c[0] for c in cast] == [10, 11]          # billing order preserved
    assert cast[0][2] == 'Protagonist'
    assert cast[0][3] == 'https://img/lead.jpg'
    assert cast[1][2] is None                       # empty string -> NULL


def test_extract_cast_skips_unusable_entries(enrich_script):
    payload = _details([
        {'id': None, 'name': 'No Id'},
        {'id': 5, 'name': '   '},
        {'id': 'not-an-int', 'name': 'Bad Id'},
        'not-a-dict',
        _member(6, 'Real', 'Role'),
    ])
    assert enrich_script._title_level_cast(payload) == [(6, 'Real', 'Role', None)]


def test_extract_cast_collapses_a_dual_role_to_the_higher_billing_position(
        enrich_script):
    """Documented in models/cast.py: uniqueness excludes character, so a dual
    role keeps the FIRST billing entry rather than two rows."""
    payload = _details([_member(7, 'Dual', 'Role A'), _member(7, 'Dual', 'Role B')])
    assert enrich_script._title_level_cast(payload) == [(7, 'Dual', 'Role A', None)]


def test_extract_cast_is_bounded(enrich_script):
    payload = _details([_member(1000 + i, 'P%d' % i) for i in range(80)])
    assert len(enrich_script._title_level_cast(payload)) == \
        enrich_script.MAX_CAST_PER_TITLE == 30


def test_extract_cast_tolerates_garbage(enrich_script):
    for bad in (None, {}, {'cast': None}, {'cast': 'nope'}, [], 'x'):
        assert enrich_script._title_level_cast(bad) == []


def test_enrich_persists_cast_and_marker(app, db, enrich_script,
                                         media_factory):
    m = media_factory()
    with app.app_context():
        outcome = enrich_script.enrich_media_item(
            db, (Person, MediaCast), m,
            lambda tmdb_id: _details([
                _member(970100, 'Lead Actor', 'Hero', 'https://img/a.jpg'),
                _member(970101, 'Supporting', 'Sidekick')]),
            datetime.utcnow(), attempts=1, base_delay=0)
    assert outcome == 'ok'
    assert m.cast_enriched_at is not None
    db.session.expire_all()
    assert [link.person.name for link in m.cast_members] == [
        'Lead Actor', 'Supporting']
    assert m.cast_members[0].character == 'Hero'
    assert m.cast_members[0].credit_order == 0
    assert m.cast_members[1].credit_order == 1
    assert m.cast_members[0].person.profile_url == 'https://img/a.jpg'


def test_enrich_enriches_tv_too(app, db, enrich_script, media_factory):
    """TV cast IS series-level evidence, unlike TV crew. See models/cast.py."""
    show = media_factory(media_type='tv', title='Severance')
    with app.app_context():
        outcome = enrich_script.enrich_media_item(
            db, (Person, MediaCast), show,
            lambda tmdb_id: _details([_member(970110, 'Series Regular', 'Mark')]),
            datetime.utcnow(), attempts=1, base_delay=0)
    assert outcome == 'ok'
    assert MediaCast.query.filter_by(media_item_id=show.id).count() == 1


def test_enrich_does_not_duplicate_on_re_run(app, db, enrich_script, media_factory):
    m = media_factory()

    def fetch(tmdb_id):
        return _details([_member(970120, 'Someone', 'Role')])

    with app.app_context():
        enrich_script.enrich_media_item(db, (Person, MediaCast), m, fetch,
                                        datetime.utcnow(), attempts=1,
                                        base_delay=0)
        enrich_script.enrich_media_item(db, (Person, MediaCast), m, fetch,
                                        datetime.utcnow(), attempts=1,
                                        base_delay=0)
    assert MediaCast.query.filter_by(media_item_id=m.id).count() == 1
    assert Person.query.filter_by(tmdb_person_id=970120).count() == 1


def test_enrich_updates_character_and_order_in_place(app, db, enrich_script,
                                                     media_factory):
    m = media_factory()
    with app.app_context():
        enrich_script.enrich_media_item(
            db, (Person, MediaCast), m,
            lambda i: _details([_member(970130, 'Renamed', 'Old Role')]),
            datetime.utcnow(), attempts=1, base_delay=0)
        # TMDb re-orders the billing list and renames the character.
        enrich_script.enrich_media_item(
            db, (Person, MediaCast), m,
            lambda i: _details([_member(970131, 'Newcomer'),
                                _member(970130, 'Renamed', 'New Role')]),
            datetime.utcnow(), attempts=1, base_delay=0)
    db.session.expire_all()
    assert MediaCast.query.filter_by(media_item_id=m.id).count() == 2
    row = MediaCast.query.filter_by(media_item_id=m.id).join(
        MediaCast.person).filter(Person.tmdb_person_id == 970130).one()
    assert row.character == 'New Role'
    assert row.credit_order == 1
    assert Person.query.filter_by(tmdb_person_id=970130).one().name == 'Renamed'


def test_enrich_never_prunes(app, db, enrich_script, media_factory):
    """A short/truncated response must not be able to delete correct rows."""
    m = media_factory()
    with app.app_context():
        enrich_script.enrich_media_item(
            db, (Person, MediaCast), m,
            lambda i: _details([_member(970140, 'A'), _member(970141, 'B')]),
            datetime.utcnow(), attempts=1, base_delay=0)
        enrich_script.enrich_media_item(
            db, (Person, MediaCast), m,
            lambda i: _details([_member(970140, 'A')]),
            datetime.utcnow(), attempts=1, base_delay=0)
    assert MediaCast.query.filter_by(media_item_id=m.id).count() == 2


def test_enrich_empty_result_marks_enriched(app, db, enrich_script,
                                            media_factory):
    m = media_factory()
    with app.app_context():
        outcome = enrich_script.enrich_media_item(
            db, (Person, MediaCast), m, lambda i: _details([]),
            datetime.utcnow(), attempts=1, base_delay=0)
    assert outcome == 'empty'
    assert m.cast_enriched_at is not None
    assert m.cast_members == []   # enriched-and-empty != unenriched


def test_enrich_failure_is_isolated_and_retryable(app, db, enrich_script,
                                                  media_factory, monkeypatch):
    _mark_all_enriched()
    bad, good = media_factory(), media_factory()
    _patch_fetch(monkeypatch, {
        bad.tmdb_id: RuntimeError('tmdb down'),
        good.tmdb_id: _details([_member(970150, 'Good Actor', 'Role')]),
    })
    with app.app_context():
        assert enrich_script.run(spacing=0) == 1
    assert good.cast_members != []
    assert bad.cast_members == []
    assert bad.cast_enriched_at is None      # NOT marked — retryable
    with app.app_context():
        pending = {m.id for m in
                   enrich_script._select_pending(db, 100)}
    assert bad.id in pending
    assert good.id not in pending


def test_enrich_retries_transient_failures_with_backoff(app, db, enrich_script,
                                                        media_factory):
    """A flaky fetch is retried; the item is enriched on the second attempt."""
    m = media_factory()
    attempts = []

    def flaky(tmdb_id):
        attempts.append(tmdb_id)
        if len(attempts) < 3:
            raise ConnectionError('connection reset by peer')
        return _details([_member(970160, 'Recovered', 'Role')])

    slept = []
    with app.app_context():
        outcome = enrich_script.enrich_media_item(
            db, (Person, MediaCast), m, flaky, datetime.utcnow(),
            attempts=3, base_delay=0.5, sleep=slept.append)
    assert outcome == 'ok'
    assert len(attempts) == 3
    # Linear backoff: 0.5 then 1.0 (base * attempt_number).
    assert slept == [0.5, 1.0]


def test_enrich_does_not_retry_a_not_found_title(app, db, enrich_script,
                                                 media_factory):
    """LookupError means the title does not exist; retrying only burns quota."""
    m = media_factory()
    calls = []

    def missing(tmdb_id):
        calls.append(tmdb_id)
        raise LookupError('Movie 1 was not found')

    with app.app_context():
        with pytest.raises(LookupError):
            enrich_script.enrich_media_item(
                db, (Person, MediaCast), m, missing, datetime.utcnow(),
                attempts=3, base_delay=0.5, sleep=lambda _s: None)
    assert calls == [m.tmdb_id]


def test_enrich_gives_up_after_the_attempt_budget(app, db, enrich_script,
                                                  media_factory):
    m = media_factory()
    calls = []

    def always_down(tmdb_id):
        calls.append(tmdb_id)
        raise RuntimeError('tmdb down')

    with app.app_context():
        with pytest.raises(RuntimeError):
            enrich_script.enrich_media_item(
                db, (Person, MediaCast), m, always_down, datetime.utcnow(),
                attempts=3, base_delay=0, sleep=lambda _s: None)
    assert len(calls) == 3
    assert m.cast_enriched_at is None


def test_enrich_selection_skips_enriched_and_is_ordered(app, db, enrich_script,
                                                        media_factory):
    fresh1, fresh2 = media_factory(), media_factory()
    fresh3 = media_factory(media_type='tv')
    done = media_factory()
    _write_marker(done)
    # Selection itself must never contact TMDb.
    with app.app_context():
        batch = enrich_script._select_pending(db, 100)
    ids = [m.id for m in batch]
    assert [m.id for m in batch] == sorted(ids)   # stable order
    assert {fresh1.id, fresh2.id, fresh3.id} <= set(ids)
    assert done.id not in ids
    assert [m.id for m in enrich_script._select_pending(db, 2)] == \
        [m.id for m in batch[:2]]


def test_enrich_selection_respects_batch_cap(app, db, enrich_script, media_factory):
    for _ in range(6):
        media_factory()
    with app.app_context():
        assert len(enrich_script._select_pending(db, 4)) == 4


def test_enrich_selection_can_scope_to_movies_or_tv(app, db, enrich_script,
                                                    media_factory):
    movie = media_factory()
    show = media_factory(media_type='tv')
    with app.app_context():
        movies = enrich_script._select_pending(db, 100, 'movie')
        shows = enrich_script._select_pending(db, 100, 'tv')
    assert movie.id in {m.id for m in movies}
    assert show.id not in {m.id for m in movies}
    assert show.id in {m.id for m in shows}
    assert movie.id not in {m.id for m in shows}


def test_enrich_run_covers_both_media_types(app, db, enrich_script, media_factory, monkeypatch):
    _mark_all_enriched()
    movie = media_factory()
    show = media_factory(media_type='tv')
    movie_calls, tv_calls = _patch_fetch(monkeypatch, {
        movie.tmdb_id: _details([_member(970170, 'Movie Actor')]),
        show.tmdb_id: _details([_member(970171, 'TV Actor')]),
    })
    with app.app_context():
        assert enrich_script.run(spacing=0) == 0
    assert movie_calls == [movie.tmdb_id]
    assert tv_calls == [show.tmdb_id]


def test_enrich_run_zero_exit_all_success(app, db, enrich_script,
                                          media_factory, monkeypatch):
    _mark_all_enriched()
    m = media_factory()
    _patch_fetch(monkeypatch, {
        m.tmdb_id: _details([_member(970180, 'Someone')])})
    with app.app_context():
        assert enrich_script.run(spacing=0) == 0


def test_enrich_dry_run_makes_no_call_and_no_write(app, db, enrich_script,
                                                   media_factory, monkeypatch):
    _mark_all_enriched()
    m = media_factory()
    monkeypatch.setattr('api.tmdb.movies.fetch_movie_details',
                        lambda *a, **kw: pytest.fail('dry run dialled TMDb'))
    with app.app_context():
        before = MediaCast.query.count()
        assert enrich_script.run(dry_run=True) == 0
        # Unchanged, not "zero": earlier tests share this session database.
        assert MediaCast.query.count() == before
    assert m.cast_enriched_at is None


def test_enrich_run_never_touches_the_network_outside_the_fetch_path(
        app, db, enrich_script, media_factory, monkeypatch):
    _mark_all_enriched()
    m = media_factory()
    _patch_fetch(monkeypatch, {
        m.tmdb_id: _details([_member(970190, 'Actor')])})

    import socket as socket_mod
    created = []

    class _Guard(socket_mod.socket):
        def __init__(self, *a, **kw):
            created.append(1)
            raise AssertionError('direct socket use outside fetch path')

    monkeypatch.setattr(socket_mod, 'socket', _Guard)
    with app.app_context():
        assert enrich_script.run(spacing=0) == 0
    assert created == []


def test_enrichment_script_is_the_only_tmdb_touchpoint_for_cast():
    """Source guard: no request path may fetch cast or person credits."""
    for path in ('api/statistics.py', 'api/taste_profile.py', 'api/for_you.py'):
        src = open(os.path.join(REPO, path)).read()
        assert 'fetch_movie_details' not in src, path
        assert 'fetch_tv_show_details' not in src, path
        assert 'append_to_response' not in src, path


def test_enrichment_is_not_invoked_by_the_deploy_workflow():
    """Cast is a large backfill. It must never run inside a deployment."""
    for name in os.listdir(os.path.join(REPO, '.github', 'workflows')):
        if not name.endswith('.yml'):
            continue
        src = open(os.path.join(REPO, '.github', 'workflows', name)).read()
        assert 'enrich_cast' not in src, (
            '%s invokes the cast backfill' % name)


# ════════════════════════════════════════════════════════════════════════════
# Consumer 1: api/statistics.py — the actors series
# ════════════════════════════════════════════════════════════════════════════

def _stats_media(db, title='Cast Movie', media_type='movie'):
    _TMDB_COUNTER['n'] += 1
    m = MediaItem(tmdb_id=_TMDB_COUNTER['n'], media_type=media_type,
                  title=title, runtime=100, genres='Drama')
    db.session.add(m)
    db.session.commit()
    return m


def _cast_member(db, media, person_id, name, character=None, order=0):
    from models.cast import MediaCast, Person
    p = Person.query.filter_by(tmdb_person_id=person_id).first()
    if p is None:
        p = Person(tmdb_person_id=person_id, name=name)
        db.session.add(p)
        db.session.flush()
    link = MediaCast(media_item_id=media.id, person_id=p.id,
                     character=character, credit_order=order)
    db.session.add(link)
    db.session.commit()
    return link


def _watch(db, user, media, day, rating=None):
    from models import DiaryEntry
    from datetime import date
    entry = DiaryEntry(user_id=user.id, media_id=media.id,
                       media_type=media.media_type,
                       watched_date=date(2026, 5, day), rating=rating)
    db.session.add(entry)
    db.session.commit()
    return entry


def test_statistics_actors_are_populated_from_persisted_cast(app, db, user):
    from api.statistics import get_statistics
    m = _stats_media(db, 'Shared Film')
    _cast_member(db, m, 970200, 'Star Actor', 'Hero')
    for day in (1, 2, 3):
        _watch(db, user, m, day)
    stats = get_statistics(user.id, year=2026)
    assert [a['name'] for a in stats['actors']] == ['Star Actor']
    assert stats['actors'][0]['watch_event_count'] == 3
    assert stats['actors'][0]['distinct_title_count'] == 1


def test_statistics_actors_aggregate_events_and_titles_per_person(app, db,
                                                                  user):
    from api.statistics import get_statistics
    a = _stats_media(db, 'Film A')
    b = _stats_media(db, 'Film B')
    other = _stats_media(db, 'Film C')
    _cast_member(db, a, 970201, 'Prolific')
    _cast_member(db, b, 970201, 'Prolific')
    _cast_member(db, a, 970202, 'Once Only')
    _cast_member(db, other, 970203, 'Unwatched Star')
    for day in (1, 2, 3, 4):
        _watch(db, user, a, 1)      # 4 events on A
    _watch(db, user, b, 2)
    _watch(db, user, b, 3)
    stats = get_statistics(user.id, year=2026)
    by_name = {row['name']: row for row in stats['actors']}
    assert by_name['Prolific']['watch_event_count'] == 6
    assert by_name['Prolific']['distinct_title_count'] == 2
    assert by_name['Once Only']['watch_event_count'] == 4
    # A title the user never watched contributes nothing (§2: diary is canonical).
    assert 'Unwatched Star' not in by_name


def test_statistics_actors_are_deterministic_and_bounded(app, db, user):
    from api.statistics import get_statistics, TOP_PEOPLE_LIMIT
    m = _stats_media(db, 'Ensemble')
    for i in range(TOP_PEOPLE_LIMIT + 5):
        _cast_member(db, m, 970300 + i, 'Actor %02d' % i, order=i)
    _watch(db, user, m, 1)
    stats = get_statistics(user.id, year=2026)
    assert len(stats['actors']) == TOP_PEOPLE_LIMIT
    first = [a['name'] for a in stats['actors']]
    second = [a['name'] for a in get_statistics(user.id, year=2026)['actors']]
    assert first == second


def test_statistics_actors_are_empty_without_cast_data(app, db, user):
    """Graceful degradation: no enrichment means no rows, not a placeholder."""
    from api.statistics import get_statistics
    m = _stats_media(db, 'Unenriched')
    _watch(db, user, m, 1)
    stats = get_statistics(user.id, year=2026)
    assert stats['actors'] == []
    assert all(row['name'] != 'Unknown' for row in stats['actors'])


def test_statistics_actors_never_fabricate_a_person(app, db, user):
    """An enriched-but-empty title must not produce an 'Unknown' entry."""
    from api.statistics import get_statistics
    m = _stats_media(db, 'No Cast')
    _write_marker(m)
    _watch(db, user, m, 1)
    assert get_statistics(user.id, year=2026)['actors'] == []


def test_statistics_actors_respect_the_date_window(app, db, user):
    from api.statistics import get_statistics
    m = _stats_media(db, 'Windowed')
    _cast_member(db, m, 970400, 'Window Actor')
    _watch(db, user, m, 1)                      # 2026-05-01
    assert [a['name'] for a in get_statistics(user.id, year=2026)['actors']] \
        == ['Window Actor']
    # A window that excludes the watch date must exclude the actor too.
    from datetime import date
    assert get_statistics(user.id, start_date=date(2026, 6, 1),
                          end_date=date(2026, 7, 1))['actors'] == []


def test_statistics_actors_use_the_person_identity_not_the_name(app, db, user):
    """A renamed person is one row, not two."""
    from api.statistics import get_statistics
    m = _stats_media(db, 'Renamed Star')
    _cast_member(db, m, 970410, 'Before Name')
    _watch(db, user, m, 1)
    from models.cast import Person
    Person.query.filter_by(tmdb_person_id=970410).one().name = 'After Name'
    db.session.commit()
    stats = get_statistics(user.id, year=2026)
    assert [a['name'] for a in stats['actors']] == ['After Name']


def test_statistics_actors_drop_a_removed_association(app, db, user):
    """Removing one cast row removes that actor from the series.

    (Deleting the MediaItem itself is not exercised here: diary_entry.media_id
    is NOT NULL, so a watched title cannot be deleted while history references
    it. The CASCADE behaviour itself is covered by the model tests above.)
    """
    from api.statistics import get_statistics
    from models.cast import MediaCast
    m = _stats_media(db, 'Recast Film')
    link = _cast_member(db, m, 970420, 'Departing Actor')
    _watch(db, user, m, 1)
    assert [a['name'] for a in get_statistics(user.id, year=2026)['actors']] \
        == ['Departing Actor']
    db.session.delete(link)
    db.session.commit()
    assert get_statistics(user.id, year=2026)['actors'] == []
    assert MediaCast.query.filter_by(id=link.id).first() is None


# ════════════════════════════════════════════════════════════════════════════
# Consumer 2: api/taste_profile.py — the actor-affinity dimension
# ════════════════════════════════════════════════════════════════════════════

def test_profile_actor_roundtrip_and_persistence(app, db, user):
    m = _stats_media(db, 'Liked Film')
    _cast_member(db, m, 970500, 'Favourite Actor', 'Lead')
    from models import MediaLike
    db.session.add(MediaLike(user_id=user.id, media_id=m.tmdb_id,
                             media_type='movie'))
    db.session.commit()

    import api.taste_profile as tp
    tp.compute_profile(user.id)
    profile = TasteProfile.query.filter_by(user_id=user.id).one()
    assert list(profile.actor_affinity) == ['Favourite Actor']
    assert profile.actor_affinity['Favourite Actor'] > 0
    # The dimension REUSES the same evidence event, so it must not inflate
    # the counters — same rule the director dimension follows.
    assert profile.signal_count == 1
    assert profile.distinct_title_count == 1


def test_profile_actor_affinity_is_a_separate_dimension(app, db, user):
    """Director and actor weights must never be mixed into one map."""
    m = _stats_media(db, 'Both People')
    _cast_member(db, m, 970510, 'The Director', order=0)
    from models import Director, MediaDirector
    d = Director(tmdb_person_id=970511, name='Also The Director')
    db.session.add(d)
    db.session.commit()
    db.session.add(MediaDirector(media_item_id=m.id, director_id=d.id))
    from models import MediaLike
    db.session.add(MediaLike(user_id=user.id, media_id=m.tmdb_id,
                             media_type='movie'))
    db.session.commit()

    import api.taste_profile as tp
    tp.compute_profile(user.id)
    profile = TasteProfile.query.filter_by(user_id=user.id).one()
    assert list(profile.actor_affinity) == ['The Director']
    assert list(profile.director_affinity) == ['Also The Director']


def test_profile_actor_preserves_negative_evidence(app, db, user):
    from models import RecommendationFeedback
    m = _stats_media(db, 'Disliked Film')
    _cast_member(db, m, 970520, 'Disliked Actor')
    db.session.add(RecommendationFeedback(
        user_id=user.id, media_id=m.tmdb_id, media_type='movie',
        event='not_interested', surface='for_you', source='web'))
    db.session.commit()

    import api.taste_profile as tp
    tp.compute_profile(user.id)
    profile = TasteProfile.query.filter_by(user_id=user.id).one()
    assert profile.actor_affinity['Disliked Actor'] < 0


def test_profile_no_cast_evidence_stays_empty(app, db, user):
    """No regression when cast data is absent."""
    m = _stats_media(db, 'No Cast Data')
    from models import MediaLike
    db.session.add(MediaLike(user_id=user.id, media_id=m.tmdb_id,
                             media_type='movie'))
    db.session.commit()
    import api.taste_profile as tp
    tp.compute_profile(user.id)
    profile = TasteProfile.query.filter_by(user_id=user.id).one()
    assert profile.actor_affinity == {}


def test_profile_actor_affinity_is_top_n_bounded(app, db, user):
    m = _stats_media(db, 'Big Ensemble')
    for i in range(20):
        _cast_member(db, m, 970600 + i, 'Extra %02d' % i, order=i)
    from models import MediaLike
    db.session.add(MediaLike(user_id=user.id, media_id=m.tmdb_id,
                             media_type='movie'))
    db.session.commit()
    import api.taste_profile as tp
    tp.compute_profile(user.id)
    profile = TasteProfile.query.filter_by(user_id=user.id).one()
    assert len(profile.actor_affinity) == tp._ACTOR_TOP_N


def test_profile_actor_affinity_is_l2_normalized(app, db, user):
    import api.taste_profile as tp
    for index in range(3):
        m = _stats_media(db, 'Norm Film %d' % index)
        _cast_member(db, m, 970700 + index, 'Actor %d' % index)
        from models import MediaLike
        db.session.add(MediaLike(user_id=user.id, media_id=m.tmdb_id,
                                 media_type='movie'))
    db.session.commit()
    tp.compute_profile(user.id)
    weights = TasteProfile.query.filter_by(user_id=user.id).one().actor_affinity
    # normalize_l2 rounds each weight to _ROUND_DIGITS, so the norm lands
    # near 1 rather than exactly on it.
    assert abs(sum(w * w for w in weights.values()) - 1.0) < 1e-3


def test_profile_computation_does_not_change_existing_dimension_scores(
        app, db, user):
    """F9 adds a dimension; it must not re-weight anything else.

    Two users, identical evidence, one with cast persisted. genre/decade/
    media_type/runtime/director must be byte-identical.
    """
    import api.taste_profile as tp

    def _seed(u, with_cast):
        m = _stats_media(db, 'Shared %s' % u.id)
        if with_cast:
            _cast_member(db, m, 970800 + u.id, 'Some Actor')
        from models import Review
        db.session.add(Review(user_id=u.id, media_id=m.id, media_type='movie',
                              rating=4.5))
        db.session.commit()
        tp.compute_profile(u.id)
        return TasteProfile.query.filter_by(user_id=u.id).one()

    from models import User

    def _user(suffix):
        u = User(username=_uid('cmp' + suffix), email=f"{_uid(suffix)}@x.io")
        u.set_password('password123')
        db.session.add(u)
        db.session.commit()
        return u

    without = _seed(_user('a'), with_cast=False)
    with_cast = _seed(_user('b'), with_cast=True)
    for dimension in ('genre_weights', 'decade_weights', 'director_affinity',
                      'media_type_pref', 'runtime_pref'):
        assert getattr(without, dimension) == getattr(with_cast, dimension), \
            dimension
    assert without.actor_affinity == {}
    assert with_cast.actor_affinity != {}


def test_profile_version_bumped_for_the_new_dimension(app, db, user):
    import api.taste_profile as tp
    assert tp.PROFILE_VERSION == 2
    m = _stats_media(db, 'Versioned')
    from models import MediaLike
    db.session.add(MediaLike(user_id=user.id, media_id=m.tmdb_id,
                             media_type='movie'))
    db.session.commit()
    tp.compute_profile(user.id)
    assert TasteProfile.query.filter_by(
        user_id=user.id).one().profile_version == 2


def test_actor_affinity_does_not_change_ranking_weights(app, db, user):
    """taste_match_score's components must still sum to 1.0 and ignore cast."""
    import api.taste_profile as tp
    assert abs(sum(tp.TASTE_MATCH_WEIGHTS.values()) - 1.0) < 1e-9
    assert 'actor' not in tp.TASTE_MATCH_WEIGHTS
    inputs = {'genre_weights': {'Drama': 1.0}, 'director_affinity': {},
              'decade_weights': {'2020s': 1.0}, 'media_type_pref':
              {'movie': 1.0}, 'runtime_pref': {}}
    before = tp.taste_match_score(inputs, {'genres': ['Drama'],
                                           'decade': '2020s',
                                           'media_type': 'movie'})
    inputs['actor_affinity'] = {'Someone': 9.0}
    after = tp.taste_match_score(inputs, {'genres': ['Drama'],
                                          'decade': '2020s',
                                          'media_type': 'movie'})
    assert before == after


def test_actor_affinity_is_exported(app, db, user):
    m = _stats_media(db, 'Exported Film')
    _cast_member(db, m, 970900, 'Exported Actor')
    from models import MediaLike
    db.session.add(MediaLike(user_id=user.id, media_id=m.tmdb_id,
                             media_type='movie'))
    db.session.commit()
    import api.taste_profile as tp
    tp.compute_profile(user.id)

    from api.account_export import taste_profile_section
    section = taste_profile_section(user.id)
    assert section is not None
    assert 'Exported Actor' in section['actor_affinity']


def test_actor_affinity_cost_is_one_batched_query(app, db, user, monkeypatch):
    """The cast dimension must not introduce an N+1."""
    import api.taste_profile as tp
    from sqlalchemy import event as sa_event

    for index in range(5):
        m = _stats_media(db, 'Batch Film %d' % index)
        _cast_member(db, m, 971000 + index, 'Batched Actor %d' % index)
        from models import MediaLike
        db.session.add(MediaLike(user_id=user.id, media_id=m.tmdb_id,
                                 media_type='movie'))
    db.session.commit()

    statements = []

    def _record(conn, cursor, statement, *args, **kwargs):
        statements.append(statement)

    sa_event.listen(db.engine, 'before_cursor_execute', _record)
    try:
        tp.compute_profile(user.id)
    finally:
        sa_event.remove(db.engine, 'before_cursor_execute', _record)

    cast_queries = [s for s in statements if 'FROM media_cast' in s]
    # One batched read per collector, never one per title: 7 collectors, and
    # each must issue at most a single media_cast read regardless of title count.
    assert 0 < len(cast_queries) <= 7, cast_queries


# ════════════════════════════════════════════════════════════════════════════
# REGRESSION — Blockers found by the F9 release audit
# ════════════════════════════════════════════════════════════════════════════


def test_regression_populated_taste_profile_survives_0003(tmp_path):
    """Blocker 1: the real production shape, not an empty table.

    `actor_affinity_json` is added with ALTER TABLE to a table that already has
    rows. A NOT NULL column with no database-side default is REFUSED outright by
    both engines ("column contains null values" on PostgreSQL, "Cannot add a
    NOT NULL column with default value NULL" on SQLite), so the pre-fix
    implementation could not migrate any database that had a single computed
    TasteProfile. Every earlier F9 migration test used an EMPTY taste_profile,
    which is why this was green and would still have failed in production.

    SQLite reproduces the failure, so this runs in ordinary CI — no PostgreSQL
    required.
    """
    import sqlite3

    from sqlalchemy import create_engine, inspect

    db_path = _pre_f9_db(tmp_path, 'populated.db', taste_profile_rows=3)

    # Precondition: the table really is populated and really lacks the column.
    conn = sqlite3.connect(str(db_path))
    before = conn.execute(
        'SELECT COUNT(*) FROM taste_profile').fetchone()[0]
    before_columns = {r[1] for r in conn.execute(
        'PRAGMA table_info(taste_profile)')}
    conn.close()
    assert before == 3, before
    assert 'actor_affinity_json' not in before_columns

    result = _run_cli(db_path, 'upgrade')
    assert result.returncode == 0, result.stdout + result.stderr

    # 0003 applied and recorded; the destructive migration did not.
    assert '+ 0003_cast_persistence' in result.stdout
    conn = sqlite3.connect(str(db_path))
    versions = [r[0] for r in conn.execute(
        'SELECT version FROM schema_migrations ORDER BY version')]
    conn.close()
    assert '0003_cast_persistence' in versions
    assert '0002_remove_legacy_wishlist' not in versions

    # Every pre-existing row survived, unchanged, with the new column
    # initialised to the empty object.
    conn = sqlite3.connect(str(db_path))
    rows = conn.execute(
        'SELECT id, user_id, genre_weights_json, decade_weights_json, '
        'director_affinity_json, runtime_pref_json, media_type_pref_json, '
        'signal_count, distinct_title_count, profile_version, '
        'actor_affinity_json '
        'FROM taste_profile ORDER BY id').fetchall()
    conn.close()
    assert len(rows) == 3
    for (row_id, user_id, genre, decade, director, runtime, media_type,
         signals, titles, version, actor) in rows:
        assert user_id == 100 + (row_id - 1)
        assert genre == '{"Drama": 0.5}'
        assert decade == '{"2010s": 0.5}'
        assert director == '{"Old Director": 1.0}'
        assert '"p75": 120' in runtime
        assert media_type == '{"movie": 0.8, "tv": 0.2}'
        assert (signals, titles, version) == (7, 6, 1)
        assert actor == '{}', (
            'existing rows must be backfilled to the empty object, got %r'
            % (actor,))

    # The resulting schema meets the chosen contract.
    engine = create_engine('sqlite:///%s' % db_path)
    columns = {c['name']: c for c in
               inspect(engine).get_columns('taste_profile')}
    engine.dispose()
    assert columns['actor_affinity_json']['nullable'] is False
    assert columns['actor_affinity_json']['default'] == "'{}'"


def test_regression_future_orm_inserts_still_work_after_0003(tmp_path, app, db):
    """Blocker 1, second half: the column must behave for NEW rows too.

    A fix that made the column nullable, or dropped the Python-side default,
    would pass the populated-table test and then break the model contract.
    """
    from models import TasteProfile, User

    user = User(username=_uid('tp'), email=f"{_uid('tp')}@x.io")
    user.set_password('password123')
    db.session.add(user)
    db.session.commit()

    profile = TasteProfile(user_id=user.id)
    db.session.add(profile)
    db.session.commit()

    fetched = TasteProfile.query.filter_by(user_id=user.id).one()
    assert fetched.actor_affinity_json == '{}'
    assert fetched.actor_affinity == {}
    fetched.actor_affinity = {'Someone': 0.5}
    db.session.commit()
    assert TasteProfile.query.filter_by(
        user_id=user.id).one().actor_affinity == {'Someone': 0.5}


def test_regression_declared_column_carries_a_server_default(app, db):
    """The invariant the populated-table ALTER depends on.

    Without a server-side default, ADD COLUMN NOT NULL cannot backfill existing
    rows on either engine. Asserted on the model so the regression is caught
    before any migration test needs a database.
    """
    from sqlalchemy.dialects import postgresql, sqlite
    from sqlalchemy.schema import CreateColumn

    column = db.metadata.tables['taste_profile'].c['actor_affinity_json']
    assert column.nullable is False
    assert column.server_default is not None, (
        'actor_affinity_json needs a server-side default so ALTER TABLE can '
        'backfill existing rows; the Python default only covers new inserts')
    assert column.default is not None, (
        'the Python-side default must stay for ORM inserts')

    for dialect in (sqlite.dialect(), postgresql.dialect()):
        rendered = str(CreateColumn(column).compile(dialect=dialect))
        assert 'NOT NULL' in rendered, rendered
        assert "DEFAULT '{}'" in rendered, (
            'without an inline DEFAULT the ADD COLUMN cannot backfill existing '
            'rows and is refused: %s' % rendered)


# ── Blocker 2: the pre-upgrade guard must tolerate exactly the F9 objects ────


def _guard(db_path, *extra):
    env = dict(os.environ)
    env.update({
        'DATABASE_URL': 'sqlite:///%s' % db_path,
        'SECRET_KEY': 'test-secret-key-for-tests',
        'TMDB_API_KEY': 'test-key',
    })
    env.pop('SKIP_SCHEMA_GUARD', None)   # the guard must actually run
    return subprocess.run(
        [sys.executable, '-m', 'utils.schema_guard', *extra],
        capture_output=True, text=True, env=env, cwd=REPO, timeout=180)


def test_regression_strict_guard_rejects_the_post_f8_pre_f9_state(tmp_path):
    """Blocker 2, first half: proves the problem was real.

    On a genuine post-F8/pre-F9 database the STRICT guard fails — that is what
    aborted the F9 deploy before `upgrade` could run.
    """
    db_path = _pre_f9_db(tmp_path, 'strict.db', taste_profile_rows=2)
    result = _guard(db_path)
    assert result.returncode == 1, result.stdout + result.stderr
    for name in ('person', 'media_cast'):
        assert name in result.stderr
    assert 'cast_enriched_at' in result.stderr
    assert 'actor_affinity_json' in result.stderr


def test_regression_pending_aware_guard_accepts_only_f9_objects(tmp_path):
    """Blocker 2, second half: the intended tolerance."""
    db_path = _pre_f9_db(tmp_path, 'pending.db', taste_profile_rows=2)
    result = _guard(db_path, '--allow-pending-migrations')
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'Schema OK' in result.stdout
    assert 'person' in result.stdout and 'media_cast' in result.stdout
    assert 'actor_affinity_json' in result.stdout


def test_regression_pending_aware_guard_still_fails_on_unrelated_drift(tmp_path):
    """Tolerance must not become leniency."""
    import sqlite3
    db_path = _pre_f9_db(tmp_path, 'drift.db')
    conn = sqlite3.connect(str(db_path))
    conn.execute('DROP TABLE media_like')      # nothing owns this
    conn.commit()
    conn.close()

    result = _guard(db_path, '--allow-pending-migrations')
    assert result.returncode == 1, result.stdout + result.stderr
    assert 'media_like' in result.stderr
    assert 'Schema OK' not in result.stdout


def test_regression_pending_aware_guard_never_excuses_an_applied_migration(
        tmp_path):
    """Once 0003 is RECORDED, its objects must exist or it is real drift.

    This is the fail-closed edge: ownership is a property of a PENDING
    migration, not of a table name.
    """
    import sqlite3
    db_path = _pre_f9_db(tmp_path, 'applied.db')
    result = _run_cli(db_path, 'upgrade')
    assert result.returncode == 0, result.stdout + result.stderr

    # The ledger now records 0003. Remove an object it created.
    conn = sqlite3.connect(str(db_path))
    conn.execute('DROP TABLE media_cast')
    conn.commit()
    conn.close()

    result = _guard(db_path, '--allow-pending-migrations')
    assert result.returncode == 1, result.stdout + result.stderr
    assert 'media_cast' in result.stderr


def test_regression_destructive_migration_owns_nothing(tmp_path):
    """0002 is deferred and destructive; it must never excuse a missing table."""
    from utils.schema_guard import pending_migration_objects
    from sqlalchemy import create_engine

    engine = create_engine('sqlite://')
    owned = pending_migration_objects(engine)
    engine.dispose()
    # 0002 declares no CREATES_TABLES, and is DESTRUCTIVE besides.
    assert 'user_wishlist' not in owned['tables']
    assert ('user_watchlist', 'anything') not in owned['columns']


def test_regression_actor_affinity_reaches_the_explanation_api(app, db, user):
    """The narrow consumer integration: describe_profile() exposes it.

    Purely additive. api/for_you.py and src/api/agent_service.py read NAMED keys
    from this dict and never iterate it, so adding a key cannot change ranking,
    explanations or CineBot context.
    """
    import api.taste_profile as tp
    m = _stats_media(db, 'Explainable Film')
    _cast_member(db, m, 970950, 'Explainable Actor')
    from models import MediaLike
    db.session.add(MediaLike(user_id=user.id, media_id=m.tmdb_id,
                             media_type='movie'))
    db.session.commit()

    profile = tp.compute_profile(user.id)
    described = tp.describe_profile(profile)
    assert 'Explainable Actor' in described['actor_affinity']
    # Still NOT a scoring input — F9 must not re-rank anyone.
    assert 'actor' not in tp.TASTE_MATCH_WEIGHTS
    assert 'actor_affinity' not in tp.taste_match_inputs(profile)


def test_regression_enrichment_rejects_bad_operator_input():
    """--spacing and --limit are validated before any work starts."""
    enrich_script = _enrich_module()

    for bad_spacing in (-1, -0.001):
        with pytest.raises(ValueError, match='spacing must be'):
            enrich_script.validate_options(10, bad_spacing)

    with pytest.raises(ValueError, match='at least 1'):
        enrich_script.validate_options(0, 0.1)
    with pytest.raises(ValueError, match='hard per-run ceiling'):
        enrich_script.validate_options(
            enrich_script.MAX_MEDIA_ITEMS_CEILING + 1, 0.1)
    with pytest.raises(ValueError, match='integer'):
        enrich_script.validate_options('abc', 0.1)

    # Valid values pass through unchanged.
    assert enrich_script.validate_options(50, 0.5) == (50, 0.5)
    assert enrich_script.validate_options(
        enrich_script.MAX_MEDIA_ITEMS, 0) == (enrich_script.MAX_MEDIA_ITEMS, 0)
