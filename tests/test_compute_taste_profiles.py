"""Tests for the nightly taste-profile recomputation job (Feature #6, Phase 3).

The job is orchestration only — computation stays in the canonical service.
Dual harness:

  SUBPROCESS — runs scripts/compute_taste_profiles.py the way an operator
  does (against a disposable SQLite DATABASE_URL). Proves exit codes 0/1/2,
  operator log format, persistence, and idempotent re-runs. (In-process
  import cannot retarget DATABASE_URL: conftest's app is already in
  sys.modules.)

  IN-PROCESS — imports run() against the hermetic session DB to prove
  batching boundaries, deterministic ordering, per-user failure isolation,
  profile preservation, cold-start neutrality, and hygiene (canonical
  compute_profile() is called; no db.create_all(); no network).
"""
import importlib.util
import os
import sqlite3
import subprocess
import sys
import uuid
from datetime import date

import pytest

from models import User, TasteProfile, MediaItem, Review

_SCRIPT_PATH = os.path.abspath(os.path.join(
    os.path.dirname(__file__), '..', 'scripts', 'compute_taste_profiles.py'))


# ════════════════════════════════════════════════════════════════════════════
# Helpers
# ════════════════════════════════════════════════════════════════════════════

def _unique(prefix):
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


def _unique_user(prefix, session):
    # Email domain is module-unique so the autouse teardown can remove
    # exactly this module's users without touching other modules' rows.
    u = User(username=_unique(prefix),
             email=f"{_unique(prefix)}@tasteprofile.test",
             email_verified=True)
    u.set_password('TestPass1')
    session.session.add(u)
    session.session.commit()
    return u


def _run_script(db_file, env_overrides=None):
    """Run the script as an operator would, against a target DATABASE_URL."""
    env = dict(os.environ)
    env.update({
        'DATABASE_URL': f'sqlite:///{db_file}',
        'SECRET_KEY': 'test-secret-key-for-tests',
        'TMDB_API_KEY': 'test-key',
        'OPENAI_API_KEY': 'test-openai-key',
        'SKIP_SCHEMA_GUARD': '1',
        'RATELIMIT_ENABLED': 'False',
        'MAIL_SERVER': '',
    })
    env.pop('RATELIMIT_STORAGE_URI', None)
    if env_overrides:
        env.update(env_overrides)
    return subprocess.run(
        [sys.executable, _SCRIPT_PATH],
        capture_output=True, text=True, env=env, timeout=180)


# User DDL for subprocess fixtures: must satisfy every NOT NULL column the
# User model selects (the canonical service loads the User row). Mirrors the
# model's real columns rather than a minimal subset.
_USER_DDL = """
    CREATE TABLE "user" (
        id INTEGER PRIMARY KEY, username TEXT UNIQUE, email TEXT UNIQUE,
        password_hash TEXT NOT NULL, date_joined DATETIME,
        is_active BOOLEAN, email_verified BOOLEAN, first_name TEXT,
        last_name TEXT, bio TEXT, profile_picture TEXT,
        total_reviews INTEGER DEFAULT 0, total_movies_watched INTEGER DEFAULT 0,
        followers_count INTEGER DEFAULT 0, following_count INTEGER DEFAULT 0,
        streaming_region TEXT);
"""


def _sqlite_file(db_file, users, with_media_and_review=True):
    """Create a legacy-shaped SQLite DB whose fixture tables (user,
    media_item, review, taste_profile) are generated from the REAL SQLAlchemy
    metadata — no hand-copied DDL to drift out of sync. The taste_profile
    table here mirrors the Phase-1 migration; startup's db.create_all() is
    NOT the mechanism that fills anything (the job provides the data)."""
    conn = sqlite3.connect(db_file)
    from models.base import db as _db
    from sqlalchemy.dialects import sqlite as sqlite_dialect
    from sqlalchemy.schema import CreateTable
    dialect = sqlite_dialect.dialect()
    for table in (_db.metadata.tables['user'],
                  _db.metadata.tables['media_item'],
                  _db.metadata.tables['review'],
                  _db.metadata.tables['taste_profile']):
        conn.execute(str(CreateTable(table).compile(dialect=dialect)) + ';')
    for i in range(users):
        conn.execute(
            'INSERT INTO "user" (username, email, password_hash,'
            ' email_verified) VALUES (?, ?, ?, 1)',
            (f'u{i}', f'u{i}@x.io', 'h'))
    if with_media_and_review and users:
        conn.execute(
            "INSERT INTO media_item (tmdb_id, media_type, title, genres,"
            " runtime) VALUES (990001, 'movie', 'Heat', 'Thriller', 110)")
        conn.execute(
            "INSERT INTO review (user_id, media_id, media_type, rating)"
            " VALUES (1, 1, 'movie', 5.0)")
    conn.commit()
    conn.close()


@pytest.fixture(autouse=True)
def _cleanup_owned_rows(db, app):
    """Remove every row this module creates, per test.

    The suite shares one session-scoped SQLite DB, and other test modules
    assume pre-existing table state: test_continue_watching deletes all
    media_item rows, and test_feed deletes media it created — SQLite then
    REUSES the freed PK ids. A review row left behind by this module would
    silently re-attach to a later module's reused media PK and blow up that
    module's teardown (FK NOT NULL during cascade). So this module must
    leave zero residue — same convention as conftest's sample_user and
    test_feed's feed_user cleanups.
    """
    yield
    with app.app_context():
        from sqlalchemy import delete as _delete
        from models import User, MediaItem, Review, TasteProfile
        from models import user_watchlist, DiaryEntry, MediaLike
        from models import UserMediaTag, UserChatDailyUsage
        ids = [r[0] for r in db.session.query(User.id).filter(
            User.email.like('%@tasteprofile.test')).all()]
        if not ids:
            return
        db.session.execute(_delete(Review).where(Review.user_id.in_(ids)))
        db.session.execute(_delete(DiaryEntry).where(
            DiaryEntry.user_id.in_(ids)))
        db.session.execute(_delete(MediaLike).where(
            MediaLike.user_id.in_(ids)))
        db.session.execute(_delete(UserMediaTag).where(
            UserMediaTag.user_id.in_(ids)))
        db.session.execute(_delete(UserChatDailyUsage).where(
            UserChatDailyUsage.user_id.in_(ids)))
        db.session.execute(user_watchlist.delete().where(
            user_watchlist.c.user_id.in_(ids)))
        db.session.execute(_delete(TasteProfile).where(
            TasteProfile.user_id.in_(ids)))
        # This module only ever uses tmdb_id >= 910000 for media fixtures.
        db.session.execute(_delete(MediaItem).where(
            MediaItem.tmdb_id >= 910000))
        db.session.execute(_delete(User).where(User.id.in_(ids)))
        db.session.commit()


@pytest.fixture(scope='module')
def script():
    """Import the script module fresh (its own _load() targets the app that
    is already in sys.modules — i.e. the hermetic test app)."""
    spec = importlib.util.spec_from_file_location(
        'compute_taste_profiles_test', _SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ════════════════════════════════════════════════════════════════════════════
# 1. zero-user database completes successfully
# ════════════════════════════════════════════════════════════════════════════

def test_zero_user_database_completes_successfully(tmp_path):
    db_file = tmp_path / 'empty.db'
    _sqlite_file(db_file, users=0, with_media_and_review=False)
    run = _run_script(db_file)
    assert run.returncode == 0, run.stdout + run.stderr
    assert 'Users: 0' in run.stdout
    assert '[DONE] success=0 failed=0 skipped=0' in run.stdout
    assert '[STATUS] OK' in run.stdout


# ════════════════════════════════════════════════════════════════════════════
# 2/3. one user recomputed; multiple users processed
# ════════════════════════════════════════════════════════════════════════════

def test_one_user_is_recomputed(app, db, script):
    user = _unique_user('one', db)
    item = MediaItem(tmdb_id=910001, media_type='movie', title='Se7en',
                     genres='Thriller, Crime', runtime=127,
                     release_date=date(1995, 9, 22))
    db.session.add(item)
    db.session.flush()
    db.session.add(Review(user_id=user.id, media_id=item.id,
                          media_type='movie', rating=5.0))
    db.session.commit()

    with app.app_context():
        stats = script.run(batch_size=10)

    assert stats['success'] == 1
    assert stats['failed'] == 0
    profiles = TasteProfile.query.filter_by(user_id=user.id).all()
    assert len(profiles) == 1
    assert profiles[0].signal_count == 1
    assert profiles[0].distinct_title_count == 1


def test_multiple_users_processed(app, db, script):
    users = [_unique_user('multi', db) for _ in range(5)]
    with app.app_context():
        stats = script.run(batch_size=2)  # 5 users → 3 batches
    assert stats['success'] >= 5
    for u in users:
        assert TasteProfile.query.filter_by(user_id=u.id).count() == 1


# ════════════════════════════════════════════════════════════════════════════
# 4/5. batch boundaries work; deterministic user ordering
# ════════════════════════════════════════════════════════════════════════════

def test_batch_boundaries_and_deterministic_ordering(app, db, script):
    users = [_unique_user('order', db) for _ in range(7)]
    observed = []
    original = script.compute_profile

    def spy(user_id):
        observed.append(user_id)
        return original(user_id)

    script.compute_profile = spy
    try:
        with app.app_context():
            stats = script.run(batch_size=3)  # 7 users → batches of 3/3/1
    finally:
        script.compute_profile = original

    assert stats['success'] >= 7
    assert observed == sorted(observed), 'must process in ascending id order'
    assert {u.id for u in users} <= set(observed)


# ════════════════════════════════════════════════════════════════════════════
# 6/7. existing profile is updated; new profile created on success
# ════════════════════════════════════════════════════════════════════════════

def test_existing_profile_updated_not_duplicated(app, db, script):
    user = _unique_user('update', db)
    item1 = MediaItem(tmdb_id=910002, media_type='movie', title='Prisoners',
                      genres='Thriller', runtime=153,
                      release_date=date(2013, 9, 20))
    db.session.add(item1)
    db.session.flush()
    db.session.add(Review(user_id=user.id, media_id=item1.id,
                          media_type='movie', rating=5.0))
    db.session.commit()

    with app.app_context():
        script.run(batch_size=10)
    first = TasteProfile.query.filter_by(user_id=user.id).one()

    # New evidence arrives between runs.
    item2 = MediaItem(tmdb_id=910003, media_type='movie', title='Zodiac',
                      genres='Thriller, Mystery', runtime=157,
                      release_date=date(2007, 3, 2))
    db.session.add(item2)
    db.session.flush()
    db.session.add(Review(user_id=user.id, media_id=item2.id,
                          media_type='movie', rating=4.0))
    db.session.commit()

    with app.app_context():
        script.run(batch_size=10)
    profiles = TasteProfile.query.filter_by(user_id=user.id).all()
    assert len(profiles) == 1, 'repeat run must update, never duplicate'
    assert profiles[0].id == first.id
    assert profiles[0].signal_count == 2
    assert profiles[0].distinct_title_count == 2


def test_new_profile_created_when_computation_succeeds(app, db, script):
    user = _unique_user('create', db)
    assert TasteProfile.query.filter_by(user_id=user.id).count() == 0
    item = MediaItem(tmdb_id=910004, media_type='movie', title='Arrival',
                     genres='Sci-Fi, Drama', runtime=116,
                     release_date=date(2016, 11, 10))
    db.session.add(item)
    db.session.flush()
    db.session.add(Review(user_id=user.id, media_id=item.id,
                          media_type='movie', rating=4.5))
    db.session.commit()

    with app.app_context():
        script.run(batch_size=10)
    assert TasteProfile.query.filter_by(user_id=user.id).count() == 1


# ════════════════════════════════════════════════════════════════════════════
# 8/9. one user's failure does not stop later users; previous profile intact
# ════════════════════════════════════════════════════════════════════════════

def test_one_user_failure_does_not_stop_later_users(app, db, script):
    good1 = _unique_user('good1', db)
    bad = _unique_user('bad', db)
    good2 = _unique_user('good2', db)
    item = MediaItem(tmdb_id=910005, media_type='movie', title='Heat',
                     genres='Thriller', runtime=170,
                     release_date=date(1995, 12, 15))
    db.session.add(item)
    db.session.flush()
    db.session.add(Review(user_id=good1.id, media_id=item.id,
                          media_type='movie', rating=5.0))
    db.session.commit()

    original = script.compute_profile

    def flaky(user_id):
        if user_id == bad.id:
            raise RuntimeError('boom: injected failure')
        return original(user_id)

    script.compute_profile = flaky
    try:
        with app.app_context():
            stats = script.run(batch_size=1)
    finally:
        script.compute_profile = original

    assert stats['failed'] == 1
    assert stats['success'] >= 2
    assert TasteProfile.query.filter_by(user_id=good1.id).count() == 1
    assert TasteProfile.query.filter_by(user_id=good2.id).count() == 1
    # The failed user got no half-written profile.
    assert TasteProfile.query.filter_by(user_id=bad.id).count() == 0


def test_failed_user_leaves_previous_valid_profile_intact(app, db, script):
    user = _unique_user('preserve', db)
    item = MediaItem(tmdb_id=910006, media_type='movie', title='Sicario',
                     genres='Thriller', runtime=121,
                     release_date=date(2015, 9, 17))
    db.session.add(item)
    db.session.flush()
    db.session.add(Review(user_id=user.id, media_id=item.id,
                          media_type='movie', rating=5.0))
    db.session.commit()

    with app.app_context():
        script.run(batch_size=10)
    good = TasteProfile.query.filter_by(user_id=user.id).one()
    assert good.signal_count == 1

    # Inject a failure for this user on the next run.
    original = script.compute_profile

    def flaky(user_id):
        if user_id == user.id:
            raise RuntimeError('boom: injected failure')
        return original(user_id)

    script.compute_profile = flaky
    try:
        with app.app_context():
            stats = script.run(batch_size=10)
    finally:
        script.compute_profile = original

    assert stats['failed'] == 1
    profiles = TasteProfile.query.filter_by(user_id=user.id).all()
    assert len(profiles) == 1
    assert profiles[0].id == good.id
    assert profiles[0].signal_count == 1, 'previous valid profile untouched'


# ════════════════════════════════════════════════════════════════════════════
# 10. batch rollback behavior
# ════════════════════════════════════════════════════════════════════════════

def test_batch_rollback_behavior(app, db, script):
    """A failure mid-batch must not leak partial session state: the failed
    user's pending insert is rolled back and later users in the same batch
    (and the next batch) still commit cleanly."""
    bad = _unique_user('rollback', db)
    good = _unique_user('rollbackgood', db)
    item = MediaItem(tmdb_id=910007, media_type='movie', title='Sully',
                     genres='Drama', runtime=96,
                     release_date=date(2016, 9, 2))
    db.session.add(item)
    db.session.flush()
    db.session.add(Review(user_id=good.id, media_id=item.id,
                          media_type='movie', rating=4.0))
    db.session.commit()

    original = script.compute_profile

    def flaky(user_id):
        if user_id == bad.id:
            raise RuntimeError('boom: mid-batch failure')
        return original(user_id)

    script.compute_profile = flaky
    try:
        with app.app_context():
            stats = script.run(batch_size=1)
    finally:
        script.compute_profile = original

    assert stats['failed'] == 1
    assert TasteProfile.query.filter_by(user_id=bad.id).count() == 0
    assert TasteProfile.query.filter_by(user_id=good.id).count() == 1


# ════════════════════════════════════════════════════════════════════════════
# 11/12/13. counts/logging correct; exit codes 0/1/2 (operator subprocess)
# ════════════════════════════════════════════════════════════════════════════

def test_exit_code_zero_counts_and_logging(tmp_path):
    db_file = tmp_path / 'ok.db'
    _sqlite_file(db_file, users=2)
    run = _run_script(db_file)
    assert run.returncode == 0, run.stdout + run.stderr
    assert '[START] Taste profile recomputation' in run.stdout
    assert 'Users: 2' in run.stdout
    assert '[DONE] success=2 failed=0 skipped=0' in run.stdout
    assert '[STATUS] OK' in run.stdout
    conn = sqlite3.connect(db_file)
    assert conn.execute('SELECT COUNT(*) FROM taste_profile').fetchone()[0] == 2
    conn.close()


def test_exit_code_one_when_profile_failures(tmp_path):
    """A user-level failure must be isolated, logged per user id, counted,
    and reflected in a non-zero exit — never hidden behind a success exit."""
    db_file = tmp_path / 'broken.db'
    conn = sqlite3.connect(db_file)
    conn.executescript(f"""
        {_USER_DDL}
        CREATE TABLE media_item (
            id INTEGER PRIMARY KEY, tmdb_id INTEGER, media_type TEXT,
            title TEXT, release_date DATE, poster_path TEXT, genres TEXT,
            overview TEXT, rating FLOAT, runtime INTEGER);
        CREATE TABLE taste_profile (
            id INTEGER PRIMARY KEY, user_id INTEGER UNIQUE NOT NULL,
            genre_weights_json TEXT, decade_weights_json TEXT,
            director_affinity_json TEXT, runtime_pref_json TEXT,
            media_type_pref_json TEXT, mood_tags_json TEXT,
            confidence REAL NOT NULL, signal_count INTEGER NOT NULL,
            distinct_title_count INTEGER NOT NULL,
            created_at DATETIME NOT NULL);
    """)
    for i in range(2):
        conn.execute(
            'INSERT INTO "user" (username, email, password_hash,'
            ' email_verified) VALUES (?, ?, ?, 1)', (f'x{i}', f'x{i}@x.io', 'h'))
    conn.commit()
    conn.close()
    # taste_profile lacks profile_version → every compute_profile() write
    # fails (model INSERT references the missing column) → 2 isolated
    # failures → exit 1. Also proves db.create_all() is NOT the mechanism:
    # a create_all() would have "repaired" the table and masked the failure.

    run = _run_script(db_file)
    assert run.returncode == 1, run.stdout + run.stderr
    assert run.stdout.count('[FAIL]') == 2
    assert 'failed=2' in run.stdout
    assert '[STATUS] FAILED' in run.stdout
    assert '[STATUS] OK' not in run.stdout
    conn = sqlite3.connect(db_file)
    assert conn.execute('SELECT COUNT(*) FROM taste_profile').fetchone()[0] == 0
    conn.close()


def test_exit_code_two_on_fatal_infrastructure_error(tmp_path):
    run = _run_script(tmp_path / 'missing.db', env_overrides={
        'DATABASE_URL': 'postgresql://nobody:nopass@127.0.0.1:1/none'})
    assert run.returncode == 2, run.stdout + run.stderr
    assert '[FATAL]' in run.stdout


# ════════════════════════════════════════════════════════════════════════════
# 14/15. no db.create_all(); canonical compute_profile() is called
# ════════════════════════════════════════════════════════════════════════════

def test_no_db_create_all():
    with open(_SCRIPT_PATH) as f:
        src = f.read()
    assert 'create_all' not in src


def test_canonical_compute_profile_is_called(app, db, script, monkeypatch):
    """The script delegates entirely to the canonical service."""
    calls = []
    monkeypatch.setattr(script, 'compute_profile',
                        lambda uid: calls.append(uid) or None)
    user = _unique_user('canonical', db)
    with app.app_context():
        script.run(batch_size=10)
    assert user.id in calls
    assert calls == sorted(calls)


# ════════════════════════════════════════════════════════════════════════════
# 16. no network calls introduced by the script
# ════════════════════════════════════════════════════════════════════════════

def test_no_network_calls_from_script_path(app, db, script, monkeypatch):
    """With all socket creation forbidden, a full run over a user with real
    evidence still succeeds — the job is local-data-only."""
    import socket

    class _NoNetwork(Exception):
        pass

    def _blocked(*args, **kwargs):
        raise _NoNetwork('network access attempted')

    monkeypatch.setattr(socket, 'socket', _blocked)
    user = _unique_user('nonet', db)
    item = MediaItem(tmdb_id=910008, media_type='movie', title='Nightcrawler',
                     genres='Thriller', runtime=117,
                     release_date=date(2014, 10, 31))
    db.session.add(item)
    db.session.flush()
    db.session.add(Review(user_id=user.id, media_id=item.id,
                          media_type='movie', rating=4.5))
    db.session.commit()

    with app.app_context():
        stats = script.run(batch_size=10)
    assert stats['failed'] == 0
    assert stats['success'] >= 1
    profile = TasteProfile.query.filter_by(user_id=user.id).one()
    assert profile.signal_count == 1


# ════════════════════════════════════════════════════════════════════════════
# 17. repeat execution is safe/idempotent
# ════════════════════════════════════════════════════════════════════════════

def test_repeat_execution_is_idempotent(tmp_path):
    db_file = tmp_path / 'repeat.db'
    _sqlite_file(db_file, users=1)
    run1 = _run_script(db_file)
    assert run1.returncode == 0, run1.stdout + run1.stderr
    conn = sqlite3.connect(db_file)
    count1 = conn.execute('SELECT COUNT(*) FROM taste_profile').fetchone()[0]
    conn.close()

    run2 = _run_script(db_file)
    assert run2.returncode == 0, run2.stdout + run2.stderr
    conn = sqlite3.connect(db_file)
    count2 = conn.execute('SELECT COUNT(*) FROM taste_profile').fetchone()[0]
    conn.close()
    assert count1 == count2 == 1


# ════════════════════════════════════════════════════════════════════════════
# 18. users with no taste evidence remain valid neutral profiles
# ════════════════════════════════════════════════════════════════════════════

def test_no_evidence_user_gets_valid_neutral_profile(app, db, script):
    user = _unique_user('cold', db)
    with app.app_context():
        stats = script.run(batch_size=10)
    assert stats['success'] >= 1
    p = TasteProfile.query.filter_by(user_id=user.id).one()
    assert p.confidence == 0.0
    assert p.signal_count == 0
    assert p.distinct_title_count == 0
    assert p.genre_weights == {}
    assert p.decade_weights == {}
    assert p.media_type_pref == {}


def test_watchlist_only_user(app, db, script):
    """Watchlist-only users stay cold-start-neutral (below the 5-title
    confidence gate) but their weak intent signal is recorded."""
    from models import user_watchlist
    user = _unique_user('wl_only', db)
    item = MediaItem(tmdb_id=910009, media_type='movie',
                     title='Blade Runner 2049', genres='Sci-Fi', runtime=164,
                     release_date=date(2017, 10, 6))
    db.session.add(item)
    db.session.flush()
    db.session.execute(user_watchlist.insert().values(
        user_id=user.id, media_id=item.id, media_type='movie'))
    db.session.commit()

    with app.app_context():
        stats = script.run(batch_size=10)
    assert stats['success'] >= 1
    p = TasteProfile.query.filter_by(user_id=user.id).one()
    assert p.signal_count == 1
    assert p.confidence == 0.0
    assert p.media_type_pref == {'movie': 1.0}
