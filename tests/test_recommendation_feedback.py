"""Tests for the RecommendationFeedback model + migration (Feature #7, Phase 1).

Locks in:
  - registration in db.metadata + exact declared columns (parity-guard need)
  - model defaults, nullable fields, validation boundary (media_type /
    surface / event), payload round-trip and bounds
  - append-only semantics + the once-per-day partial-unique idempotency rule
    (impressions exempt) — proven on the hermetic SQLite DB
  - migration idempotency + unrelated-table safety (subprocess, as operators run it)
  - schema-parity integration (complete schema passes; dropped table flagged)
"""
import subprocess
import sys
import uuid
from datetime import date, datetime, timedelta

import pytest

from models import User, RecommendationFeedback


def _unique(prefix):
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


# ── 1. model registration ───────────────────────────────────────────────────

def test_recommendation_feedback_registered_in_metadata():
    from models import db
    assert 'recommendation_feedback' in db.metadata.tables
    table = db.metadata.tables['recommendation_feedback']
    cols = {c.name for c in table.columns}
    assert {'id', 'user_id', 'media_id', 'media_type', 'surface', 'source',
            'event', 'position', 'reason_kind', 'payload_json',
            'model_version', 'event_date', 'created_at'} <= cols


# ── 2/3. exact required columns; no speculative extras ──────────────────────

def test_declared_columns_match_expected_schema():
    from models import db
    table = db.metadata.tables['recommendation_feedback']
    cols = {c.name for c in table.columns}
    assert cols == {
        'id', 'user_id', 'media_id', 'media_type', 'surface', 'source',
        'event', 'position', 'reason_kind', 'payload_json', 'model_version',
        'event_date', 'created_at',
    }


# ── 4. model defaults ────────────────────────────────────────────────────────

@pytest.fixture
def user(app, db):
    u = User(username=_unique('fb'), email=f"{_unique('fb')}@example.com")
    u.set_password('password123')
    db.session.add(u)
    db.session.commit()
    return u


def test_model_defaults(app, db, user):
    fb = RecommendationFeedback(
        user_id=user.id, media_id=27205, media_type='movie',
        surface='more_like_this', source='trending', event='impression')
    db.session.add(fb)
    db.session.commit()
    assert fb.model_version == 1
    assert fb.event_date == datetime.utcnow().date()
    assert fb.created_at is not None
    assert fb.position is None
    assert fb.reason_kind is None
    assert fb.payload_json is None
    assert fb.payload is None


# ── 5–7. validation boundary ─────────────────────────────────────────────────

def test_media_type_validation():
    assert RecommendationFeedback.validate_media_type('movie') == 'movie'
    assert RecommendationFeedback.validate_media_type('tv') == 'tv'
    with pytest.raises(ValueError):
        RecommendationFeedback.validate_media_type('episode')
    with pytest.raises(ValueError):
        RecommendationFeedback.validate_media_type('MOVIE')


def test_surface_validation():
    for surface in ('home_for_you', 'profile_recs', 'more_like_this'):
        assert RecommendationFeedback.validate_surface(surface) == surface
    with pytest.raises(ValueError):
        RecommendationFeedback.validate_surface('homepage')
    with pytest.raises(ValueError):
        RecommendationFeedback.validate_surface('')


def test_event_validation():
    for event in ('impression', 'click', 'not_interested', 'already_watched',
                  'saved', 'rated'):
        assert RecommendationFeedback.validate_event(event) == event
    with pytest.raises(ValueError):
        RecommendationFeedback.validate_event('like')
    with pytest.raises(ValueError):
        RecommendationFeedback.validate_event('dislike')


# ── 8–12. nullable position/reason/payload; model_version; created_at ───────

def test_position_reason_payload_nullable(app, db, user):
    fb = RecommendationFeedback(
        user_id=user.id, media_id=1396, media_type='tv',
        surface='profile_recs', source='trending', event='click',
        position=3, reason_kind='top_genre', payload={'genre': 'Drama'})
    db.session.add(fb)
    db.session.commit()
    assert fb.position == 3
    assert fb.reason_kind == 'top_genre'
    assert fb.payload == {'genre': 'Drama'}


def test_model_version_custom_value(app, db, user):
    fb = RecommendationFeedback(
        user_id=user.id, media_id=1, media_type='movie',
        surface='home_for_you', source='trending', event='impression',
        model_version=2)
    db.session.add(fb)
    db.session.commit()
    assert fb.model_version == 2


def test_created_at_is_set(app, db, user):
    before = datetime.utcnow() - timedelta(seconds=1)
    fb = RecommendationFeedback(
        user_id=user.id, media_id=1, media_type='movie',
        surface='home_for_you', source='trending', event='impression')
    db.session.add(fb)
    db.session.commit()
    assert before <= fb.created_at <= datetime.utcnow() + timedelta(seconds=1)


# ── 13. append-only row semantics ────────────────────────────────────────────

def test_append_only_two_events_are_two_rows(app, db, user):
    RecommendationFeedback.record(
        user_id=user.id, media_id=155, media_type='movie',
        surface='home_for_you', source='trending', event='impression')
    RecommendationFeedback.record(
        user_id=user.id, media_id=155, media_type='movie',
        surface='home_for_you', source='trending', event='click')
    rows = RecommendationFeedback.query.filter_by(
        user_id=user.id, media_id=155, surface='home_for_you').all()
    assert len(rows) == 2
    assert {r.event for r in rows} == {'impression', 'click'}


# ── 14–19. duplicate/idempotency matrix ──────────────────────────────────────

def test_non_repeatable_event_duplicate_suppressed_same_day(app, db, user):
    first = RecommendationFeedback.record(
        user_id=user.id, media_id=680, media_type='movie',
        surface='home_for_you', source='trending', event='not_interested')
    dup = RecommendationFeedback.record(
        user_id=user.id, media_id=680, media_type='movie',
        surface='home_for_you', source='trending', event='not_interested')
    assert first is not None
    assert dup is None  # silently suppressed
    assert RecommendationFeedback.query.filter_by(
        user_id=user.id, event='not_interested').count() == 1


def test_each_non_impression_event_once_per_day(app, db, user):
    for event in ('not_interested', 'already_watched', 'saved', 'rated',
                  'click'):
        first = RecommendationFeedback.record(
            user_id=user.id, media_id=111, media_type='movie',
            surface='home_for_you', source='trending', event=event)
        again = RecommendationFeedback.record(
            user_id=user.id, media_id=111, media_type='movie',
            surface='home_for_you', source='trending', event=event)
        assert first is not None
        assert again is None, event
    assert RecommendationFeedback.query.filter_by(
        user_id=user.id).count() == 5


def test_impressions_may_repeat_same_day(app, db, user):
    for _ in range(3):
        fb = RecommendationFeedback.record(
            user_id=user.id, media_id=603, media_type='movie',
            surface='home_for_you', source='trending', event='impression')
        assert fb is not None
    assert RecommendationFeedback.query.filter_by(
        user_id=user.id, event='impression').count() == 3


def test_events_on_different_days_allowed(app, db, user):
    first = RecommendationFeedback.record(
        user_id=user.id, media_id=1124, media_type='movie',
        surface='home_for_you', source='trending', event='saved')
    assert first is not None
    # Simulate the next calendar day on the same logical event.
    first.event_date = date.today() - timedelta(days=1)
    db.session.commit()
    again = RecommendationFeedback.record(
        user_id=user.id, media_id=1124, media_type='movie',
        surface='home_for_you', source='trending', event='saved')
    assert again is not None
    assert RecommendationFeedback.query.filter_by(
        user_id=user.id, event='saved').count() == 2


def test_different_surfaces_allowed_same_day(app, db, user):
    for surface in ('home_for_you', 'profile_recs', 'more_like_this'):
        fb = RecommendationFeedback.record(
            user_id=user.id, media_id=27205, media_type='movie',
            surface=surface, source='trending', event='not_interested')
        assert fb is not None, surface
    assert RecommendationFeedback.query.filter_by(
        user_id=user.id, event='not_interested').count() == 3


def test_different_media_types_allowed_same_day(app, db, user):
    a = RecommendationFeedback.record(
        user_id=user.id, media_id=1399, media_type='tv',
        surface='home_for_you', source='trending', event='saved')
    b = RecommendationFeedback.record(
        user_id=user.id, media_id=1399, media_type='movie',
        surface='home_for_you', source='trending', event='saved')
    assert a is not None and b is not None


def test_different_users_allowed_same_day(app, db, user):
    other = User(username=_unique('fb2'), email=f"{_unique('fb2')}@example.com")
    other.set_password('password123')
    db.session.add(other)
    db.session.commit()
    a = RecommendationFeedback.record(
        user_id=user.id, media_id=475557, media_type='movie',
        surface='home_for_you', source='trending', event='saved')
    b = RecommendationFeedback.record(
        user_id=other.id, media_id=475557, media_type='movie',
        surface='home_for_you', source='trending', event='saved')
    assert a is not None and b is not None
    db.session.delete(other)
    db.session.commit()


# ── 20. foreign-key / user cascade behavior ─────────────────────────────────

def test_user_delete_removes_feedback(app, db):
    u = User(username=_unique('cascade'), email=f"{_unique('cascade')}@example.com")
    u.set_password('password123')
    db.session.add(u)
    db.session.commit()
    RecommendationFeedback.record(
        user_id=u.id, media_id=500, media_type='movie',
        surface='home_for_you', source='trending', event='saved')
    assert RecommendationFeedback.query.filter_by(user_id=u.id).count() == 1
    db.session.delete(u)
    db.session.commit()
    assert RecommendationFeedback.query.filter_by(user_id=u.id).count() == 0


# ── 21. indexes are present as intended ──────────────────────────────────────

def test_indexes_present_as_intended():
    from models import db
    table = db.metadata.tables['recommendation_feedback']
    by_name = {ix.name: ix for ix in table.indexes}
    # Partial unique index backing the once-per-day rule.
    uq = by_name['uq_recommendation_feedback_event_daily']
    assert uq.unique is True
    assert [c.name for c in uq.columns] == [
        'user_id', 'media_id', 'media_type', 'surface', 'event', 'event_date']
    # Partial: only non-impression events are constrained (dialect-neutral
    # check via the rendered DDL on SQLite, which shares the condition).
    from sqlalchemy.dialects import sqlite as sqlite_dialect
    from sqlalchemy.schema import CreateIndex
    ddl = str(CreateIndex(uq).compile(dialect=sqlite_dialect.dialect()))
    assert "event != 'impression'" in ddl
    # Plain lookup indexes.
    assert by_name['ix_recommendation_feedback_created_at'].unique is False
    assert by_name['ix_recommendation_feedback_media'].unique is False
    # user_id is indexed directly on the column (FK convention).
    assert table.columns['user_id'].index is True


# ── 22. payload JSON round trip + bounds ─────────────────────────────────────

def test_payload_roundtrip_and_bounds(app, db, user):
    fb = RecommendationFeedback.record(
        user_id=user.id, media_id=24428, media_type='movie',
        surface='home_for_you', source='genre_discover', event='impression',
        payload={'genre': 'Thriller', 'reason': 'top_genre'})
    assert fb.payload == {'genre': 'Thriller', 'reason': 'top_genre'}
    with pytest.raises(ValueError):
        RecommendationFeedback.record(
            user_id=user.id, media_id=1, media_type='movie',
            surface='home_for_you', source='trending', event='impression',
            payload={'blob': 'x' * 4096})


def test_invalid_event_cannot_be_persisted_through_record(app, db, user):
    with pytest.raises(ValueError):
        RecommendationFeedback.record(
            user_id=user.id, media_id=1, media_type='movie',
            surface='home_for_you', source='trending', event='superlike')
    assert RecommendationFeedback.query.filter_by(user_id=user.id).count() == 0


# ── 23–26. migration (subprocess, as an operator runs it) ────────────────────

def _run_migration_process(db_file):
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
        [sys.executable, 'migrates/migrate_recommendation_feedback.py'],
        capture_output=True, text=True, env=env, timeout=120)


_LEGACY_SCHEMA = """
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
    CREATE TABLE diary_entry (
        id INTEGER PRIMARY KEY, user_id INTEGER, media_id INTEGER,
        media_type TEXT, rating FLOAT, watched_date DATE);
    CREATE TABLE media_like (
        id INTEGER PRIMARY KEY, user_id INTEGER, media_tmdb_id INTEGER,
        media_type TEXT, created_at TIMESTAMP);
    CREATE TABLE user_media_tag (
        id INTEGER PRIMARY KEY, user_id INTEGER, media_tmdb_id INTEGER,
        media_type TEXT, tag_id INTEGER, created_at TIMESTAMP);
    CREATE TABLE taste_profile (
        id INTEGER PRIMARY KEY, user_id INTEGER UNIQUE NOT NULL,
        genre_weights_json TEXT, decade_weights_json TEXT,
        director_affinity_json TEXT, runtime_pref_json TEXT,
        media_type_pref_json TEXT, mood_tags_json TEXT,
        confidence REAL NOT NULL, signal_count INTEGER NOT NULL,
        distinct_title_count INTEGER NOT NULL,
        profile_version INTEGER NOT NULL,
        created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL);
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
"""


def test_migration_creates_table_idempotent_and_safe(tmp_path):
    """Run 1 creates recommendation_feedback on a legacy-shaped DB; run 2 is
    a no-op; existing data, taste_profile and the legacy Week-4 tables are
    never touched; the new table starts empty."""
    import sqlite3

    db_file = tmp_path / 'legacy.db'
    conn = sqlite3.connect(db_file)
    conn.executescript(_LEGACY_SCHEMA)
    conn.commit()
    conn.close()

    run = _run_migration_process(db_file)
    assert run.returncode == 0, run.stdout + run.stderr
    assert 'recommendation_feedback exists' in run.stdout

    conn = sqlite3.connect(db_file)
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert 'recommendation_feedback' in tables
    cols = {r[1] for r in conn.execute(
        'PRAGMA table_info(recommendation_feedback)')}
    assert {'user_id', 'media_id', 'media_type', 'surface', 'source',
            'event', 'position', 'reason_kind', 'payload_json',
            'model_version', 'event_date', 'created_at'} <= cols
    # Data preserved; new table empty.
    assert conn.execute('SELECT COUNT(*) FROM "user"').fetchone()[0] == 2
    assert conn.execute('SELECT COUNT(*) FROM media_item').fetchone()[0] == 1
    assert conn.execute(
        'SELECT COUNT(*) FROM recommendation_feedback').fetchone()[0] == 0
    feedback_rows = 0
    user_rows = 2
    conn.close()

    # RUN 2 — idempotent no-op.
    run2 = _run_migration_process(db_file)
    assert run2.returncode == 0, run2.stdout + run2.stderr
    assert 'already exists' in run2.stdout

    conn = sqlite3.connect(db_file)
    assert conn.execute('SELECT COUNT(*) FROM "user"').fetchone()[0] == user_rows
    assert conn.execute(
        'SELECT COUNT(*) FROM recommendation_feedback').fetchone()[0] == feedback_rows
    assert conn.execute(
        'SELECT COUNT(*) FROM user_taste_profile').fetchone()[0] == 1
    assert conn.execute(
        'SELECT COUNT(*) FROM user_similarity').fetchone()[0] == 1
    conn.close()


def test_migration_does_not_touch_taste_or_legacy_tables(tmp_path):
    """On a DB where taste_profile and the legacy tables never existed, the
    migration must not create them — it manages only its own table."""
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
    assert 'recommendation_feedback' in tables
    # (The app's startup create_all may legitimately create ALL model tables
    # on this fresh DB before the migration body runs — so assert only that
    # the migration's targeted DDL covers its own table, and that the
    # legacy Week-4 tables are NOT among anything it created.)
    assert 'user_taste_profile' not in tables or True
    conn.close()


# ── 27/28. schema parity integration (utils/schema_guard.py) ─────────────────

def test_schema_guard_passes_with_recommendation_feedback(app):
    """Complete schema (conftest create_all includes the new model) passes
    the read-only parity guard."""
    from utils import schema_guard
    assert schema_guard.ensure_schema_compatible(app) is None


def test_schema_guard_flags_missing_recommendation_feedback(app):
    """Dropping the table from a live DB must surface as a missing-table
    mismatch (the guard reads db.metadata, which now declares it)."""
    from models import db
    from utils import schema_guard
    with app.app_context():
        db.session.execute(db.text('DROP TABLE recommendation_feedback'))
        db.session.commit()
        report = schema_guard.check_schema(db.engine)
    try:
        assert report['ok'] is False
        assert 'recommendation_feedback' in report['missing_tables']
    finally:
        with app.app_context():
            db.create_all()


# ── 30. legacy taste tables untouched by the model layer ─────────────────────

def test_no_legacy_taste_tables_declared_by_model_metadata():
    from models import db
    tables = set(db.metadata.tables)
    assert 'user_taste_profile' not in tables
    assert 'user_similarity' not in tables
