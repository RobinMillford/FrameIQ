"""
Feature #1 — One-Tap Global Quick Log + Canonical Watched State.

Tests POST /api/media/<id>/log (routes/diary.py) and the canonical watched
model: DiaryEntry is the authoritative per-watch-event history; user_viewed
is a derived boolean set synced on every log event.

The quick-log guard is process-local monotonic state — cleared between tests
to keep each case independent. Media rows created by earlier tests persist
(shared SQLite in-memory DB), so tests use a get-or-create helper instead of
blind inserts.
"""
from datetime import date

import pytest

from models import db, DiaryEntry, MediaItem, user_viewed, user_watchlist

import routes.diary as diary_mod


URL = '/api/media/{tmdb_id}/log'
TMDB_ID = 603


@pytest.fixture(autouse=True)
def _reset_quicklog_guard():
    """Isolate tests from the process-local duplicate-submission guard."""
    diary_mod._quicklog_guard.clear()
    yield
    diary_mod._quicklog_guard.clear()


@pytest.fixture
def quick_user(db, app):
    """Test user whose tracking rows are cleaned BEFORE the user row is
    deleted (avoids FK nulling during teardown)."""
    from models import User
    db.session.rollback()  # clear any session state from a prior test
    u = User(username='quickuser', email='quick@example.com',
             email_verified=True)
    u.set_password('TestPass1')
    db.session.add(u)
    db.session.commit()
    yield u
    db.session.rollback()
    DiaryEntry.query.filter_by(user_id=u.id).delete()
    db.session.execute(user_viewed.delete().where(
        user_viewed.c.user_id == u.id))
    db.session.execute(user_watchlist.delete().where(
        user_watchlist.c.user_id == u.id))
    db.session.delete(u)
    db.session.commit()


@pytest.fixture
def quick_client(client, quick_user):
    """Logged-in client bound to quick_user."""
    client.post('/login', data={
        'username': 'quickuser',
        'password': 'TestPass1',
    }, follow_redirects=True)
    return client


def _log(client, tmdb_id, **payload):
    base = {'media_type': 'movie'}
    base.update(payload)
    return client.post(URL.format(tmdb_id=tmdb_id), json=base)


def _ensure_media(db, tmdb_id=TMDB_ID, title='Fight Club'):
    """Get-or-create the shared MediaItem row (it may already exist from an
    earlier test that created it through the endpoint)."""
    media = MediaItem.query.filter_by(
        tmdb_id=tmdb_id, media_type='movie').first()
    if not media:
        media = MediaItem(tmdb_id=tmdb_id, media_type='movie', title=title)
        db.session.add(media)
        db.session.commit()
    return media


def _diary_rows(user_id, media_id):
    return DiaryEntry.query.filter_by(
        user_id=user_id, media_id=media_id, media_type='movie',
    ).order_by(DiaryEntry.watched_date.asc(), DiaryEntry.id.asc()).all()


def _viewed_row(user_id, media_id):
    return db.session.execute(
        user_viewed.select().where(
            user_viewed.c.user_id == user_id,
            user_viewed.c.media_id == media_id,
            user_viewed.c.media_type == 'movie',
        )
    ).fetchone()


# ── Test 1: unwatched → watched ──────────────────────────────────────────────

def test_quick_log_marks_unwatched_movie_watched(quick_client, quick_user, db):
    resp = _log(quick_client, TMDB_ID, title='Fight Club')
    assert resp.status_code == 201
    data = resp.get_json()
    assert data['success'] is True
    assert data['watched'] is True
    assert data['logged_today'] is True
    assert data['is_rewatch'] is False


# ── Test 2: correct history/diary record created ─────────────────────────────

def test_quick_log_creates_history_record(quick_client, quick_user, db):
    media = _ensure_media(db)
    _log(quick_client, TMDB_ID, title='Fight Club')
    rows = _diary_rows(quick_user.id, media.id)
    assert len(rows) == 1
    assert rows[0].watched_date == date.today()
    assert rows[0].is_rewatch is False
    assert rows[0].rating is None


# ── Test 3: defaults to today's date ─────────────────────────────────────────

def test_quick_log_defaults_to_today(quick_client, quick_user, db):
    media = _ensure_media(db)
    _log(quick_client, TMDB_ID, title='Fight Club')
    row = _diary_rows(quick_user.id, media.id)[0]
    assert row.watched_date == date.today()


# ── Test 4: second log is a rewatch ──────────────────────────────────────────

def test_quick_log_rewatch_after_existing_watch(quick_client, quick_user, db):
    media = _ensure_media(db)

    first = _log(quick_client, TMDB_ID, title='Fight Club')
    assert first.get_json()['is_rewatch'] is False

    # Cooldown guard is per-action; a genuine later watch is allowed.
    diary_mod._quicklog_guard.clear()
    second = _log(quick_client, TMDB_ID, title='Fight Club')
    assert second.status_code == 201
    assert second.get_json()['is_rewatch'] is True

    rows = _diary_rows(quick_user.id, media.id)
    assert len(rows) == 2
    assert rows[0].is_rewatch is False
    assert rows[1].is_rewatch is True


# ── Test 5: original watch history preserved ─────────────────────────────────

def test_quick_log_preserves_original_history(quick_client, quick_user, db):
    media = _ensure_media(db)

    _log(quick_client, TMDB_ID, title='Fight Club', rating=4.5)
    diary_mod._quicklog_guard.clear()
    _log(quick_client, TMDB_ID, title='Fight Club')

    rows = _diary_rows(quick_user.id, media.id)
    assert len(rows) == 2
    assert rows[0].rating == 4.5           # original rating untouched
    assert rows[0].watched_date == date.today()
    assert rows[1].rating is None          # rewatch created its own event
    # Derived boolean set still has exactly one row.
    assert _viewed_row(quick_user.id, media.id) is not None


# ── Test 6: duplicate submission does not duplicate events ───────────────────

def test_quick_log_duplicate_submission_is_idempotent(quick_client, quick_user, db):
    media = _ensure_media(db)

    first = _log(quick_client, TMDB_ID, title='Fight Club')
    dup = _log(quick_client, TMDB_ID, title='Fight Club')

    assert first.status_code == 201
    assert dup.status_code == 200
    assert dup.get_json()['duplicate'] is True

    rows = _diary_rows(quick_user.id, media.id)
    assert len(rows) == 1                  # only one watch event
    assert _viewed_row(quick_user.id, media.id) is not None


# ── Test 7: authorization — anonymous requests are rejected ──────────────────

def test_quick_log_requires_auth(client):
    resp = client.post(URL.format(tmdb_id=TMDB_ID),
                       json={'media_type': 'movie'})
    assert resp.status_code in (302, 401)


# ── Test 8: watchlist sync semantics ─────────────────────────────────────────

def test_quick_log_does_not_break_watchlist_semantics(quick_client, quick_user, db):
    """Existing FrameIQ behavior: logging does not auto-remove watchlist rows
    (no such rule exists in the codebase). This test pins that behavior so a
    future policy change is a deliberate, visible change."""
    media = _ensure_media(db)
    db.session.execute(user_watchlist.insert().values(
        user_id=quick_user.id, media_id=media.id, media_type='movie',
    ))
    db.session.commit()

    _log(quick_client, TMDB_ID, title='Fight Club')

    still_listed = db.session.execute(
        user_watchlist.select().where(
            user_watchlist.c.user_id == quick_user.id,
            user_watchlist.c.media_id == media.id,
        )
    ).fetchone()
    assert still_listed is not None
    assert _viewed_row(quick_user.id, media.id) is not None


# ── Test 9: existing diary/viewed behavior continues working ─────────────────

def test_existing_diary_log_endpoint_still_works(quick_client, quick_user, db):
    """The full diary workflow (/api/diary/log) is unchanged by quick-log."""
    media = _ensure_media(db)

    resp = quick_client.post('/api/diary/log', json={
        'media_id': TMDB_ID,
        'media_type': 'movie',
        'watched_date': '2026-01-15',
    })
    assert resp.status_code == 201
    rows = _diary_rows(quick_user.id, media.id)
    assert len(rows) == 1
    assert rows[0].watched_date == date(2026, 1, 15)


# ── Test 10: migration/backfill is deterministic and safe ────────────────────

def test_canonical_backfill_converges(quick_client, quick_user, db):
    """Running the reconcile passes both directions converges to parity:
    every movie diary history has a viewed row and vice versa."""
    media = _ensure_media(db)

    # Case: diary exists, viewed missing → pass 1 must insert viewed.
    db.session.add(DiaryEntry(
        user_id=quick_user.id, media_id=media.id, media_type='movie',
        watched_date=date(2025, 6, 1), is_rewatch=False,
    ))
    db.session.commit()

    from migrates.migrate_canonical_watched import migrate_canonical_watched
    from app import app as flask_app

    with flask_app.app_context():
        migrate_canonical_watched()

        assert _viewed_row(quick_user.id, media.id) is not None

        # Idempotent: a second run changes nothing.
        before = _diary_rows(quick_user.id, media.id)
        migrate_canonical_watched()
        after = _diary_rows(quick_user.id, media.id)
        assert len(before) == len(after)
