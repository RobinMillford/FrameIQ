"""Tests for the TasteProfile model + migration (Feature #6, Phase 1).

Locks in:
  - model round-trips (JSON docs, confidence/counts, timestamps/version)
  - one-profile-per-user uniqueness
  - registration in db.metadata (schema parity guard requirement)
  - migration idempotency + data preservation + legacy-table safety
"""
import subprocess
import sys
import uuid

import pytest

from models import db, User, TasteProfile


def _unique(prefix):
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


# ── Model registration (schema parity guard requirement) ────────────────────

def test_taste_profile_registered_in_metadata():
    assert 'taste_profile' in db.metadata.tables
    table = db.metadata.tables['taste_profile']
    cols = {c.name for c in table.columns}
    assert {'user_id', 'genre_weights_json', 'decade_weights_json',
            'director_affinity_json', 'runtime_pref_json',
            'media_type_pref_json', 'mood_tags_json', 'confidence',
            'signal_count', 'distinct_title_count', 'profile_version',
            'updated_at'} <= cols


def test_declared_columns_match_expected_schema():
    """Model declares exactly the audited V1 fields — no speculative extras."""
    table = db.metadata.tables['taste_profile']
    cols = {c.name for c in table.columns}
    assert cols == {
        'id', 'user_id', 'genre_weights_json', 'decade_weights_json',
        'director_affinity_json', 'runtime_pref_json',
        'media_type_pref_json', 'mood_tags_json', 'confidence',
        'signal_count', 'distinct_title_count', 'profile_version',
        'created_at', 'updated_at',
    }


# ── Model behavior (uses the hermetic SQLite session fixture) ───────────────

@pytest.fixture
def user(app, db):
    u = User(username=_unique('taster'), email=f"{_unique('taster')}@example.com")
    u.set_password('password123')
    db.session.add(u)
    db.session.commit()
    return u


def test_create_and_roundtrip(app, db, user):
    profile = TasteProfile(user_id=user.id)
    profile.genre_weights = {'Thriller': 12.5, 'Drama': 8.4}
    profile.decade_weights = {'2010s': 8.0, '1990s': 5.5}
    profile.director_affinity = {'Denis Villeneuve': 3.0}
    profile.runtime_pref = {'p25': 95, 'p75': 140, 'sample_count': 17}
    profile.media_type_pref = {'movie': 0.6, 'tv': 0.4}
    profile.confidence = 0.62
    profile.signal_count = 47
    profile.distinct_title_count = 21
    db.session.add(profile)
    db.session.commit()

    fetched = TasteProfile.query.filter_by(user_id=user.id).one()
    assert fetched.genre_weights == {'Thriller': 12.5, 'Drama': 8.4}
    assert fetched.decade_weights == {'2010s': 8.0, '1990s': 5.5}
    assert fetched.director_affinity == {'Denis Villeneuve': 3.0}
    assert fetched.runtime_pref == {'p25': 95, 'p75': 140, 'sample_count': 17}
    assert fetched.media_type_pref == {'movie': 0.6, 'tv': 0.4}
    assert fetched.confidence == pytest.approx(0.62)
    assert fetched.signal_count == 47
    assert fetched.distinct_title_count == 21


def test_json_field_defaults(app, db, user):
    profile = TasteProfile(user_id=user.id)
    db.session.add(profile)
    db.session.commit()
    fetched = db.session.get(TasteProfile, profile.id)
    assert fetched.genre_weights == {}
    assert fetched.decade_weights == {}
    assert fetched.director_affinity == {}
    assert fetched.runtime_pref == {}
    assert fetched.media_type_pref == {}
    assert fetched.mood_tags is None  # deferred dimension stays null
    assert fetched.profile_version == 1
    assert fetched.confidence == 0.0
    assert fetched.signal_count == 0
    assert fetched.distinct_title_count == 0


def test_user_id_uniqueness(app, db, user):
    db.session.add(TasteProfile(user_id=user.id))
    db.session.commit()
    db.session.add(TasteProfile(user_id=user.id))
    with pytest.raises(Exception):
        db.session.commit()
    db.session.rollback()
    # Scoped to this test's user — the suite shares one database.
    assert TasteProfile.query.filter_by(user_id=user.id).count() == 1


def test_mood_tags_roundtrip(app, db, user):
    profile = TasteProfile(user_id=user.id, mood_tags=['slow-burn', 'heist'])
    db.session.add(profile)
    db.session.commit()
    fetched = db.session.get(TasteProfile, profile.id)
    assert fetched.mood_tags == ['slow-burn', 'heist']
    # Setting None clears the column back to null (not the string 'null').
    fetched.mood_tags = None
    db.session.commit()
    assert db.session.get(TasteProfile, profile.id).mood_tags is None


def test_timestamps_and_version_defaults(app, db, user):
    profile = TasteProfile(user_id=user.id)
    db.session.add(profile)
    db.session.commit()
    assert profile.created_at is not None
    assert profile.updated_at is not None
    assert profile.profile_version == 1


def test_cascade_delete_with_user(app, db, user):
    db.session.add(TasteProfile(user_id=user.id))
    db.session.commit()
    db.session.delete(user)
    db.session.commit()
    assert TasteProfile.query.filter_by(user_id=user.id).count() == 0


# ── Migration (run against a disposable SQLite file DB) ─────────────────────

def _run_migration_process(db_file):
    """Run migrates/migrate_taste_profile.py the way an operator does —
    as a subprocess against a target DATABASE_URL. (Importing the module
    in-process cannot work here: conftest's `app` is already in sys.modules
    with its own DATABASE_URL, and create_app() runs at import time.)"""
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
        [sys.executable, 'migrates/migrate_taste_profile.py'],
        capture_output=True, text=True, env=env, timeout=120)


def test_migration_creates_table_idempotent_and_safe(tmp_path):
    """End-to-end on a pre-existing (legacy-shaped) database:
    run 1 creates taste_profile; run 2 is a no-op; user/media data and the
    legacy user_taste_profile/user_similarity tables are never touched."""
    import sqlite3

    db_file = tmp_path / 'legacy.db'
    conn = sqlite3.connect(db_file)
    # Minimal legacy schema: pre-existing tables with data, including the
    # Week-4 legacy tables. The migration must not disturb any of them.
    conn.executescript("""
        CREATE TABLE "user" (
            id INTEGER PRIMARY KEY, username TEXT, email TEXT,
            password_hash TEXT, is_active BOOLEAN,
            date_joined TIMESTAMP, email_verified BOOLEAN);
        INSERT INTO "user" (username, email, password_hash)
            VALUES ('alice', 'a@x.io', 'hash'), ('bob', 'b@x.io', 'hash');
        CREATE TABLE media_item (
            id INTEGER PRIMARY KEY, tmdb_id INTEGER, media_type TEXT,
            title TEXT, release_date DATE, poster_path TEXT, genres TEXT,
            overview TEXT, rating FLOAT, runtime INTEGER);
        INSERT INTO media_item (tmdb_id, media_type, title)
            VALUES (238, 'movie', 'The Godfather');
        CREATE TABLE user_taste_profile (
            id INTEGER PRIMARY KEY, user_id INTEGER, favorite_genres TEXT,
            avg_rating FLOAT, total_watched INTEGER, total_reviews INTEGER,
            decade_preferences TEXT, top_rated_count INTEGER,
            updated_at TIMESTAMP);
        INSERT INTO user_taste_profile (user_id) VALUES (1);
        CREATE TABLE user_similarity (
            id INTEGER PRIMARY KEY, user_id_1 INTEGER, user_id_2 INTEGER,
            similarity_score FLOAT, common_movies INTEGER, common_likes INTEGER,
            rating_correlation FLOAT, calculated_at TIMESTAMP);
        INSERT INTO user_similarity (user_id_1, user_id_2) VALUES (1, 2);
    """)
    conn.commit()
    conn.close()

    # RUN 1 — table exists afterwards with the expected columns. Note: on
    # this app, `from app import app` inside the migration triggers the
    # documented startup db.create_all(), which may create the table before
    # the migration body runs — the migration must be correct either way.
    run = _run_migration_process(db_file)
    assert run.returncode == 0, run.stdout + run.stderr
    assert 'taste_profile exists' in run.stdout
    conn = sqlite3.connect(db_file)
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert 'taste_profile' in tables
    cols = {r[1] for r in conn.execute('PRAGMA table_info(taste_profile)')}
    assert {'user_id', 'genre_weights_json', 'decade_weights_json',
            'director_affinity_json', 'runtime_pref_json',
            'media_type_pref_json', 'mood_tags_json', 'confidence',
            'signal_count', 'distinct_title_count', 'profile_version',
            'created_at', 'updated_at'} <= cols
    # Existing data preserved; nothing populated into the new table.
    assert conn.execute('SELECT COUNT(*) FROM "user"').fetchone()[0] == 2
    assert conn.execute('SELECT COUNT(*) FROM media_item').fetchone()[0] == 1
    assert conn.execute('SELECT COUNT(*) FROM taste_profile').fetchone()[0] == 0
    legacy_taste = conn.execute(
        'SELECT COUNT(*) FROM user_taste_profile').fetchone()[0]
    legacy_sim = conn.execute(
        'SELECT COUNT(*) FROM user_similarity').fetchone()[0]
    conn.close()

    # RUN 2 — idempotent no-op.
    run2 = _run_migration_process(db_file)
    assert run2.returncode == 0, run2.stdout + run2.stderr
    assert 'already exists' in run2.stdout

    # Post-run 2 invariants: legacy rows byte-identical, new table empty.
    conn = sqlite3.connect(db_file)
    assert conn.execute('SELECT COUNT(*) FROM "user"').fetchone()[0] == 2
    assert conn.execute('SELECT COUNT(*) FROM media_item').fetchone()[0] == 1
    assert conn.execute(
        'SELECT COUNT(*) FROM user_taste_profile').fetchone()[0] == legacy_taste
    assert conn.execute(
        'SELECT COUNT(*) FROM user_similarity').fetchone()[0] == legacy_sim
    assert conn.execute('SELECT COUNT(*) FROM taste_profile').fetchone()[0] == 0
    conn.close()


def test_migration_never_writes_legacy_tables(tmp_path):
    """The migration must not create/populate/drop user_taste_profile or
    user_similarity — on a DB where they never existed they must still not
    appear afterwards."""
    import sqlite3

    db_file = tmp_path / 'fresh.db'
    conn = sqlite3.connect(db_file)
    conn.executescript("""
        CREATE TABLE "user" (
            id INTEGER PRIMARY KEY, username TEXT, email TEXT,
            password_hash TEXT);
        INSERT INTO "user" (username, email, password_hash)
            VALUES ('carol', 'c@x.io', 'hash');
    """)
    conn.commit()
    conn.close()

    run = _run_migration_process(db_file)
    assert run.returncode == 0, run.stdout + run.stderr

    conn = sqlite3.connect(db_file)
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert 'taste_profile' in tables
    assert 'user_taste_profile' not in tables
    assert 'user_similarity' not in tables
    conn.close()


# ── Schema parity integration (utils/schema_guard.py) ───────────────────────

def test_schema_guard_passes_with_taste_profile(app):
    """On a complete schema (conftest's create_all), the read-only parity
    guard must accept the database that includes taste_profile."""
    from utils import schema_guard
    assert schema_guard.ensure_schema_compatible(app) is None


def test_schema_guard_flags_missing_taste_profile_table(app, monkeypatch):
    """Dropping taste_profile from a live DB must make the parity guard
    report it as a missing table (the guard reads db.metadata, which now
    includes the declared model)."""
    from models import db
    from utils import schema_guard
    with app.app_context():
        db.session.execute(db.text('DROP TABLE taste_profile'))
        db.session.commit()
        report = schema_guard.check_schema(db.engine)
    try:
        assert report['ok'] is False
        assert 'taste_profile' in report['missing_tables']
    finally:
        with app.app_context():
            db.create_all()
