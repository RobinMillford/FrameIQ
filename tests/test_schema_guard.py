"""Tests for the read-only schema parity guard (utils/schema_guard.py).

Background: db.create_all() creates NEW tables but never ALTERs existing
ones — the root cause of the media_item.runtime production incident. The
guard compares SQLAlchemy's declared model metadata against the LIVE
database schema and fails startup on drift. These tests lock in:

  - complete schema passes
  - missing table / missing column produce clear, actionable mismatches
  - extra database columns do NOT fail (legacy/manual columns tolerated)
  - the guard never issues schema-mutating SQL (read-only proof)
  - SKIP_SCHEMA_GUARD escape hatch works
  - MediaItem.runtime (the incident column) is part of expected metadata
"""
import pytest
from sqlalchemy import create_engine, event

from utils import schema_guard
from utils.schema_guard import SchemaMismatchError, check_schema


# ── 1. Complete schema → guard passes ────────────────────────────────────────

def test_complete_schema_passes(app):
    from models import db
    with app.app_context():
        db.create_all()  # hermetic temp-file SQLite from conftest
        report = check_schema(db.engine)
    assert report["ok"] is True
    assert report["missing_tables"] == []
    assert report["missing_columns"] == {}


# ── 2. Missing column → clear mismatch (the media_item.runtime case) ────────

def test_missing_column_is_detected(app):
    from models import db
    with app.app_context():
        engine = db.engine
        # Recreate media_item WITHOUT runtime — the exact legacy prod state.
        db.session.execute(db.text(
            "CREATE TABLE media_item_broken AS "
            "SELECT id, tmdb_id, media_type, title, release_date, poster_path, "
            "genres, overview, rating FROM media_item"))
        db.session.execute(db.text("DROP TABLE media_item"))
        db.session.execute(db.text(
            "ALTER TABLE media_item_broken RENAME TO media_item"))
        db.session.commit()
        try:
            report = check_schema(engine)
        finally:
            # Restore the full schema for other tests in this DB.
            db.session.execute(db.text("DROP TABLE media_item"))
            db.session.commit()
            db.create_all()
    assert report["ok"] is False
    assert report["missing_columns"] == {"media_item": ["runtime"]}


def test_missing_column_raises_startup_error(app, monkeypatch):
    # conftest sets SKIP_SCHEMA_GUARD=1 for the suite (test bootstrap is a
    # legitimate non-production context); these tests exercise the guard
    # itself, so the escape hatch is cleared — and auto-restored.
    monkeypatch.delenv("SKIP_SCHEMA_GUARD", raising=False)
    from models import db
    with app.app_context():
        db.session.execute(db.text(
            "CREATE TABLE media_item_broken AS "
            "SELECT id, tmdb_id, media_type, title FROM media_item"))
        db.session.execute(db.text("DROP TABLE media_item"))
        db.session.execute(db.text(
            "ALTER TABLE media_item_broken RENAME TO media_item"))
        db.session.commit()
        try:
            with pytest.raises(SchemaMismatchError) as excinfo:
                schema_guard.ensure_schema_compatible(app)
        finally:
            db.session.execute(db.text("DROP TABLE media_item"))
            db.session.commit()
            db.create_all()
    message = str(excinfo.value)
    assert "media_item.runtime" in message
    assert "migration" in message.lower()


# ── 3. Missing table → clear mismatch ────────────────────────────────────────

def test_missing_table_is_detected(app):
    from models import db
    with app.app_context():
        engine = db.engine
        db.session.execute(db.text("DROP TABLE smart_list"))
        db.session.commit()
        try:
            report = check_schema(engine)
        finally:
            db.create_all()
    assert report["ok"] is False
    assert "smart_list" in report["missing_tables"]


def test_missing_table_message_names_table(app, monkeypatch):
    monkeypatch.delenv("SKIP_SCHEMA_GUARD", raising=False)
    from models import db
    with app.app_context():
        db.session.execute(db.text("DROP TABLE smart_list"))
        db.session.commit()
        try:
            with pytest.raises(SchemaMismatchError) as excinfo:
                schema_guard.ensure_schema_compatible(app)
        finally:
            db.create_all()
    assert "smart_list" in str(excinfo.value)
    assert "Missing tables" in str(excinfo.value)


# ── 4. Extra database column → does NOT fail ────────────────────────────────

def test_extra_column_tolerated(app):
    from models import db
    with app.app_context():
        db.session.execute(
            db.text("ALTER TABLE media_item ADD COLUMN legacy_notes TEXT"))
        db.session.commit()
        try:
            report = check_schema(db.engine)
            assert report["ok"] is True
            assert report["missing_columns"] == {}
        finally:
            db.session.execute(
                db.text("ALTER TABLE media_item DROP COLUMN legacy_notes"))
            db.session.commit()


# ── 5. SQLite test architecture stays supported ─────────────────────────────

def test_guard_runs_against_sqlite_without_error(app, monkeypatch):
    monkeypatch.delenv("SKIP_SCHEMA_GUARD", raising=False)
    with app.app_context():
        # conftest temp-file SQLite; must introspect cleanly end-to-end.
        report = schema_guard.ensure_schema_compatible(app)
    assert report is not None and report["ok"] is True  # guard ran and passed


# ── 6. The guard NEVER mutates schema ────────────────────────────────────────

def test_guard_issues_no_ddl(app, monkeypatch):
    monkeypatch.delenv("SKIP_SCHEMA_GUARD", raising=False)
    from models import db
    with app.app_context():
        engine = db.engine

        statements = []

        def _record(conn, cursor, statement, parameters, *_a, **_kw):
            statements.append(statement.strip().upper())

        event.listen(engine, "before_cursor_execute", _record)
        try:
            schema_guard.ensure_schema_compatible(app)
        finally:
            event.remove(engine, "before_cursor_execute", _record)

        forbidden = ("ALTER ", "DROP ", "CREATE ", "INSERT ", "UPDATE ",
                     "DELETE ", "TRUNCATE ", "GRANT ")
        mutating = [s for s in statements if s.startswith(forbidden)]
        assert mutating == [], f"guard executed mutating SQL: {mutating}"
        # And it genuinely introspected rather than doing nothing.
        assert any(s.startswith("PRAGMA") for s in statements) or statements


# ── 7. Expected metadata includes MediaItem.runtime ─────────────────────────

def test_declared_metadata_contains_media_runtime():
    declared = schema_guard._declared_schema()
    assert "media_item" in declared
    assert "runtime" in declared["media_item"]


# ── 8. SKIP_SCHEMA_GUARD escape hatch ────────────────────────────────────────

def test_skip_env_var_disables_guard(app, monkeypatch):
    from models import db
    with app.app_context():
        db.session.execute(db.text("DROP TABLE smart_list"))
        db.session.commit()
        try:
            monkeypatch.setenv("SKIP_SCHEMA_GUARD", "1")
            # Drifted DB, yet no exception raised.
            assert schema_guard.ensure_schema_compatible(app) is None
        finally:
            monkeypatch.delenv("SKIP_SCHEMA_GUARD", raising=False)
            db.create_all()


# ── 9. Fresh-engine path (no tables at all) reports every declared table ────

def test_empty_database_reports_all_missing_tables():
    engine = create_engine("sqlite://")
    report = check_schema(engine)
    assert report["ok"] is False
    assert "media_item" in report["missing_tables"]
    assert "smart_list" in report["missing_tables"]
    assert "user" in report["missing_tables"]
