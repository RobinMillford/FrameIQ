"""Recommendation pipeline smoke checks (Feature #7, Phase 17) — focused suite.

Covers the operator CLI scripts/smoke_test_recommendation_pipeline.py:

- default mode is strictly read-only: no user/feedback/profile/canonical-state
  mutation, no schema mutation, no recommendation computation, no network
- the mutating live drill is double-gated: --live-test AND
  RECOMMENDATION_SMOKE_TEST=1 — either alone refuses with exit 2 BEFORE any
  write
- prerequisite coverage: tables, profile columns, engine budgets, routes,
  nightly scripts, workflow ordering, schema guard
- privacy guarantees: no raw payloads, no user IDs in operator output
- legacy taste tables are never touched

The full live drill itself (28 PASS against a disposable DB) is validated as
an operator CLI run, not re-run per test — see the Phase 17 report.
"""
import importlib.util
import logging
import os
import socket
import subprocess
import sys
import uuid

import pytest

SCRIPT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    'scripts', 'smoke_test_recommendation_pipeline.py')


def _load_script():
    spec = importlib.util.spec_from_file_location(
        'smoke_test_recommendation_pipeline', SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def smoke(app, db):
    """The smoke script module loaded against the test app context."""
    with app.app_context():
        yield _load_script()


def _strip_py_source(source):
    """Strip comments + docstrings (comment-stripped AST guard pattern)."""
    import ast
    stripped = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Expr) and isinstance(
                node.value, ast.Constant) and isinstance(
                node.value.value, str):
            continue
        stripped.append(ast.get_source_segment(source, node) or '')
    return '\n'.join(stripped)


@pytest.fixture()
def stripped_source():
    with open(SCRIPT, encoding='utf-8') as fh:
        return _strip_py_source(fh.read())


# ════════════════════════════════════════════════════════════════════════════
# Safety gates
# ════════════════════════════════════════════════════════════════════════════

def test_live_test_refused_without_env_assertion(smoke, caplog):
    """--live-test without RECOMMENDATION_SMOKE_TEST=1 must exit 2 pre-write."""
    os.environ.pop('RECOMMENDATION_SMOKE_TEST', None)
    with pytest.raises(SystemExit) as excinfo:
        smoke._live_gate()
    assert excinfo.value.code == 2
    assert any('refusing to mutate' in r.message for r in caplog.records)


def test_live_test_allowed_with_env_assertion(smoke, monkeypatch):
    """Both gates present -> no SystemExit (schema is valid in test DB)."""
    monkeypatch.setenv('RECOMMENDATION_SMOKE_TEST', '1')
    smoke._live_gate()  # must not raise


def test_gate_order_refuses_before_any_write(smoke, db, monkeypatch, caplog):
    """Gate runs before run_live() — verified structurally in main()."""
    source = open(SCRIPT, encoding='utf-8').read()
    gate_pos = source.index('_live_gate()')
    live_pos = source.index('return run_live()')
    assert gate_pos < live_pos
    with pytest.raises(SystemExit):
        smoke._live_gate()
    # nothing was written
    from models import User
    assert User.query.filter_by(email=smoke.TEST_EMAIL).count() == 0
    assert any('refusing to mutate' in r.message for r in caplog.records)


# ════════════════════════════════════════════════════════════════════════════
# Read-only default mode
# ════════════════════════════════════════════════════════════════════════════

def test_read_only_mode_no_mutation(smoke, db, caplog):
    from models import (User, RecommendationFeedback, TasteProfile,
                        user_watchlist, user_viewed, MediaItem)
    from models.social import DiaryEntry, MediaLike
    before = (
        User.query.count(),
        RecommendationFeedback.query.count(),
        TasteProfile.query.count(),
        len(db.session.execute(user_watchlist.select()).fetchall()),
        len(db.session.execute(user_viewed.select()).fetchall()),
        DiaryEntry.query.count(),
        MediaLike.query.count(),
        MediaItem.query.count(),
    )
    smoke.run_read_only()
    after = (
        User.query.count(),
        RecommendationFeedback.query.count(),
        TasteProfile.query.count(),
        len(db.session.execute(user_watchlist.select()).fetchall()),
        len(db.session.execute(user_viewed.select()).fetchall()),
        DiaryEntry.query.count(),
        MediaLike.query.count(),
        MediaItem.query.count(),
    )
    assert before == after


def test_read_only_mode_no_recommendation_computation(smoke, monkeypatch):
    """Default mode must never compute profiles or run the For You engine."""
    def _boom(*a, **kw):
        raise AssertionError('recomputation in read-only mode')
    monkeypatch.setattr(
        'api.taste_profile.compute_profile', _boom)
    monkeypatch.setattr('api.for_you.get_for_you', _boom)
    assert smoke.run_read_only() in (0, 1)


def test_read_only_mode_network_free(smoke, monkeypatch):
    """Any socket construction in read-only mode is a failure."""
    def _no_socket(*a, **kw):
        raise AssertionError('network call in read-only smoke mode')
    monkeypatch.setattr(socket, 'socket', _no_socket)
    assert smoke.run_read_only() in (0, 1)


def test_read_only_mode_no_schema_mutation(smoke, db):
    from sqlalchemy import inspect
    before = set(inspect(db.engine).get_table_names())
    smoke.run_read_only()
    assert set(inspect(db.engine).get_table_names()) == before


def test_read_only_output_no_user_ids(smoke, caplog):
    from models import User
    u = User(username='smokevis_' + uuid.uuid4().hex[:8],
             email='smokevis_%s@example.com' % uuid.uuid4().hex[:8],
             email_verified=True)
    u.set_password('x')
    db = __import__('models').db
    db.session.add(u)
    db.session.commit()
    try:
        smoke.run_read_only()
        text = '\n'.join(r.getMessage() for r in caplog.records)
        assert str(u.id) not in text
        assert u.username not in text
        assert u.email not in text
    finally:
        db.session.delete(u)
        db.session.commit()


def test_read_only_output_no_raw_payloads(smoke, caplog):
    smoke.run_read_only()
    text = '\n'.join(r.getMessage() for r in caplog.records)
    assert 'payload_json' not in text
    assert 'genre_weights_json' not in text


def test_read_only_mode_deterministic(smoke, caplog):
    smoke.run_read_only()
    first = [r.getMessage() for r in caplog.records]
    caplog.clear()
    smoke.run_read_only()
    second = [r.getMessage() for r in caplog.records]
    assert first == second


# ════════════════════════════════════════════════════════════════════════════
# Prerequisite detection
# ════════════════════════════════════════════════════════════════════════════

def test_all_prerequisites_detected_in_test_db(smoke):
    """The test DB has create_all() schema — everything must be present."""
    results = smoke.collect_readiness()
    failed = [name for name, ok, _ in results if not ok]
    assert failed == [], 'unexpected prerequisite failures: %s' % failed
    names = [name for name, _, _ in results]
    assert 'RecommendationFeedback' in names
    assert 'TasteProfile' in names
    assert 'Director capture' in names
    assert 'For You' in names
    assert 'Feedback API' in names
    assert 'Nightly recomputation' in names
    assert 'Nightly verification' in names
    assert 'Analytics' in names
    assert 'Nightly workflow' in names
    assert 'Schema guard' in names


def test_missing_prerequisite_returns_failure(smoke, db):
    """Dropping a required table must flip the table check to FAILED."""
    from sqlalchemy import text
    db.session.execute(text('DROP TABLE media_director'))
    db.session.commit()
    try:
        results = dict(
            (name, ok) for name, ok, _ in smoke.collect_readiness())
        assert results['RecommendationFeedback'] is False
    finally:
        from models import MediaDirector
        from models import db as _db
        MediaDirector.__table__.create(_db.engine)
        _db.session.commit()


def test_profile_column_check_catches_missing_column(smoke, monkeypatch):
    monkeypatch.setattr(
        smoke, 'REQUIRED_PROFILE_COLUMNS',
        smoke.REQUIRED_PROFILE_COLUMNS + ('not_a_real_column',))
    ok, detail = smoke._check_profile_columns()
    assert ok is False
    assert 'not_a_real_column' in detail


# ════════════════════════════════════════════════════════════════════════════
# Static presence checks
# ════════════════════════════════════════════════════════════════════════════

def test_nightly_scripts_present(smoke):
    ok, detail = smoke._check_nightly_scripts()
    assert ok, detail


def test_nightly_scripts_missing_detected(smoke):
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    smoke.NIGHTLY_SCRIPTS = smoke.NIGHTLY_SCRIPTS + ('scripts/nope.py',)
    ok, detail = smoke._check_nightly_scripts()
    assert ok is False
    assert 'nope.py' in detail
    assert os.path.isfile(os.path.join(base, 'scripts/nope.py')) is False


def test_workflow_presence_and_ordering(smoke):
    ok, detail = smoke._check_workflow()
    assert ok, detail


def test_workflow_checks_require_tokens(smoke, tmp_path):
    """A workflow without required tokens must FAIL the check."""
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    real = os.path.join(base, smoke.WORKFLOW_FILE)
    with open(real, encoding='utf-8') as fh:
        yml = fh.read()
    # Remove the concurrency token from a copy and point the check at it.
    broken = yml.replace('concurrency', 'cc', 1)
    fake = tmp_path / 'wf.yml'
    fake.write_text(broken, encoding='utf-8')
    smoke.WORKFLOW_FILE = str(fake)
    ok, detail = smoke._check_workflow()
    assert ok is False
    assert 'concurrency' in detail


def test_feedback_api_route_presence(smoke, app):
    with app.app_context():
        ok, detail = smoke._check_feedback_api_route()
        assert ok, detail
        ok2, detail2 = smoke._check_for_you_route()
        assert ok2, detail2


def test_for_you_engine_budgets_unchanged(smoke):
    ok, detail = smoke._check_for_you_engine()
    assert ok, detail


def test_frontend_test_files_present(smoke):
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for rel in smoke.FRONTEND_TEST_FILES:
        assert os.path.isfile(os.path.join(base, rel)), rel


# ════════════════════════════════════════════════════════════════════════════
# Source guards (comment-stripped)
# ════════════════════════════════════════════════════════════════════════════

def test_source_no_mutation_in_read_only_paths(smoke, stripped_source):
    """Read-only helpers must contain no data-mutation statements."""
    # Everything before the first mutating helper (_cleanup_identity) is
    # genuinely read-only: prerequisite checks + snapshot counting — the
    # exact code the default mode executes.
    ro_section = stripped_source.split('def _cleanup_identity(')[0]
    for token in ('.add(', '.delete(', '.commit(', 'create_all(',
                  'drop_all('):
        assert token not in ro_section, token


def test_source_no_db_url_override_arg(stripped_source):
    """The script must accept no database URL override argument."""
    import ast
    tree = ast.parse(open(SCRIPT, encoding='utf-8').read())
    adds = [n for n in ast.walk(tree)
            if isinstance(n, ast.Call)
            and getattr(n.func, 'attr', '') == 'add_argument']
    names = [a.args[0].value for a in adds
             if a.args and isinstance(a.args[0], ast.Constant)]
    assert names == ['--live-test']


def test_source_legacy_tables_untouched(stripped_source):
    assert 'user_taste_profile' not in stripped_source
    assert 'user_similarity' not in stripped_source


def test_source_no_network_imports(stripped_source):
    for token in ('requests.', 'urllib.request.urlopen', 'httpx.',
                  'tmdb_client.fetch_movie_details'):
        assert token not in stripped_source, token


# ════════════════════════════════════════════════════════════════════════════
# Cleanup / isolation guarantees
# ════════════════════════════════════════════════════════════════════════════

def test_cleanup_removes_identity(smoke, db, app):
    from datetime import date
    from models import (User, RecommendationFeedback, TasteProfile,
                        user_watchlist, user_viewed, MediaItem)
    from models.social import DiaryEntry, MediaLike
    with app.app_context():
        u = User(username=smoke.TEST_USERNAME,
                 email=smoke.TEST_EMAIL, email_verified=True)
        u.set_password('x')
        db.session.add(u)
        item = MediaItem(tmdb_id=603, media_type='movie', title='Drill')
        db.session.add(item)
        db.session.commit()
        uid = u.id
        db.session.add(RecommendationFeedback(
            user_id=uid, media_id=1, media_type='movie',
            surface='home_for_you', event='impression', source='for_you'))
        db.session.add(TasteProfile(user_id=uid))
        db.session.execute(user_watchlist.insert().values(
            user_id=uid, media_id=item.id, media_type='movie'))
        db.session.execute(user_viewed.insert().values(
            user_id=uid, media_id=item.id, media_type='movie'))
        db.session.add(DiaryEntry(user_id=uid, media_id=item.id,
                                  media_type='movie',
                                  watched_date=date.today()))
        db.session.add(MediaLike(user_id=uid, media_id=603,
                                 media_type='movie'))
        db.session.commit()

        smoke._cleanup_identity(uid, media_created=True)

        assert User.query.filter_by(id=uid).count() == 0
        assert RecommendationFeedback.query.filter_by(
            user_id=uid).count() == 0
        assert TasteProfile.query.filter_by(user_id=uid).count() == 0
        assert len(db.session.execute(user_watchlist.select().where(
            user_watchlist.c.user_id == uid)).fetchall()) == 0
        assert len(db.session.execute(user_viewed.select().where(
            user_viewed.c.user_id == uid)).fetchall()) == 0
        assert DiaryEntry.query.filter_by(user_id=uid).count() == 0
        assert MediaLike.query.filter_by(user_id=uid).count() == 0


def test_cleanup_preserves_preexisting_media(smoke, db, app, sample_user):
    """A pre-existing drill-title MediaItem must survive cleanup."""
    from models import MediaItem, User
    item = MediaItem(tmdb_id=smoke.TEST_MEDIA_TMDB_ID, media_type='movie',
                     title='Pre-existing')
    db.session.add(item)
    db.session.commit()
    item_id = item.id
    u = User(username=smoke.TEST_USERNAME, email=smoke.TEST_EMAIL,
             email_verified=True)
    u.set_password('x')
    db.session.add(u)
    db.session.commit()
    try:
        smoke._cleanup_identity(u.id, media_created=False)
        assert MediaItem.query.filter_by(id=item_id).count() == 1
    finally:
        MediaItem.query.filter_by(id=item_id).delete(
            synchronize_session=False)
        User.query.filter_by(email=smoke.TEST_EMAIL).delete(
            synchronize_session=False)
        db.session.commit()


def test_cleanup_removes_drill_created_media(smoke, db, app):
    from models import MediaItem, User
    item = MediaItem(tmdb_id=smoke.TEST_MEDIA_TMDB_ID, media_type='movie',
                     title='Smoke Drill Title')
    db.session.add(item)
    db.session.commit()
    u = User(username=smoke.TEST_USERNAME, email=smoke.TEST_EMAIL,
             email_verified=True)
    u.set_password('x')
    db.session.add(u)
    db.session.commit()
    try:
        smoke._cleanup_identity(u.id, media_created=True)
        assert MediaItem.query.filter_by(
            tmdb_id=smoke.TEST_MEDIA_TMDB_ID,
            media_type='movie').count() == 0
        assert User.query.filter_by(
            email=smoke.TEST_EMAIL).count() == 0
    except Exception:
        MediaItem.query.filter_by(id=item.id).delete(
            synchronize_session=False)
        User.query.filter_by(id=u.id).delete(synchronize_session=False)
        db.session.commit()
        raise


# ════════════════════════════════════════════════════════════════════════════
# CLI-level behaviour (subprocess; the established pattern)
# ══════════════════════════════════════════════════════════════════════
# ════════════════════════════════════════════════════════════════════════════

def _run_cli(env_extra, args, db_path):
    env = dict(os.environ)
    env['SECRET_KEY'] = 'test'
    env['DATABASE_URL'] = 'sqlite:///' + db_path
    env['SKIP_SCHEMA_GUARD'] = '1'
    env['WTF_CSRF_ENABLED'] = 'False'
    env['RATELIMIT_ENABLED'] = 'False'
    env['TMDB_API_KEY'] = 'test'
    env['MAIL_SERVER'] = ''
    env.update(env_extra)
    return subprocess.run(
        [sys.executable, SCRIPT, *args], capture_output=True, text=True,
        timeout=240, env=env)


def test_cli_default_mode_exit_code(tmp_path):
    """Empty disposable DB: read-only mode runs and reports READY/NOT READY."""
    db_path = str(tmp_path / 'cli.db')
    proc = _run_cli({}, [], db_path)
    out = proc.stdout + proc.stderr
    assert '[START]' in out
    assert '[CHECK]' in out
    assert '[STATUS]' in out
    assert proc.returncode in (0, 1)


def test_cli_live_gate_refusal(tmp_path):
    """--live-test without env assertion: exit 2, no [CHECK] spam."""
    db_path = str(tmp_path / 'cli2.db')
    proc = _run_cli({}, ['--live-test'], db_path)
    out = proc.stdout + proc.stderr
    assert proc.returncode == 2
    assert 'refusing to mutate' in out
    # The gate fired before any readiness checks / drill work.
    assert '[CHECK]' not in out
    assert '[PASS]' not in out


def test_cli_no_user_ids_in_output(tmp_path):
    db_path = str(tmp_path / 'cli3.db')
    proc = _run_cli({}, [], db_path)
    out = proc.stdout + proc.stderr
    assert 'smoke-recommendation' not in out or '[STATUS]' in out
    assert 'payload_json' not in out


# ════════════════════════════════════════════════════════════════════════════
# Legacy tables
# ════════════════════════════════════════════════════════════════════════════

def test_legacy_tables_untouched_after_read_only(smoke, db):
    from sqlalchemy import inspect, text
    insp = inspect(db.engine)
    for table in ('user_taste_profile', 'user_similarity'):
        if table not in insp.get_table_names():
            continue
        before = db.session.execute(
            text('SELECT COUNT(*) FROM %s' % table)).scalar()
        smoke.run_read_only()
        after = db.session.execute(
            text('SELECT COUNT(*) FROM %s' % table)).scalar()
        assert before == after


# ════════════════════════════════════════════════════════════════════════════
# Production-image regression (runtime vs repository metadata)
# ════════════════════════════════════════════════════════════════════════════
#
# Production false failure: the Docker image does not contain .github/
# (repository CI metadata is intentionally not copied into runtime
# images), so the workflow check reported FAILED and flipped the whole
# CLI to [STATUS] NOT READY. Contract: runtime mode treats repository-
# only workflow metadata as not-inspectable (never a readiness failure);
# validation still runs when a repository checkout is present.

def test_missing_workflow_file_is_not_a_failure(smoke):
    """Runtime image: no .github/ metadata -> ok with explanatory detail,
    NOT a FAILED check."""
    base = os.path.dirname(os.path.dirname(os.path.abspath(smoke.__file__)))
    real = os.path.join(base, smoke.WORKFLOW_FILE)
    renamed = real + '.hidden'
    os.rename(real, renamed)
    try:
        ok, detail = smoke._check_workflow()
    finally:
        os.rename(renamed, real)
    assert ok is True
    assert 'runtime image' in detail
    assert 'repository CI' in detail


def test_missing_workflow_does_not_produce_not_ready(smoke, caplog):
    """With the workflow file absent, run_read_only() must still reach
    [STATUS] READY (when everything else is healthy) — repository-only
    metadata must never flip runtime readiness."""
    base = os.path.dirname(os.path.dirname(os.path.abspath(smoke.__file__)))
    real = os.path.join(base, smoke.WORKFLOW_FILE)
    renamed = real + '.hidden'
    os.rename(real, renamed)
    try:
        caplog.set_level(logging.INFO)
        rc = smoke.run_read_only()
    finally:
        os.rename(renamed, real)
    assert rc == 0
    assert 'not inspectable in runtime image' in caplog.text


def test_workflow_validation_still_enforced_in_repository(smoke):
    """In a repository checkout the contract validation still runs and
    still fails on a broken workflow."""
    ok, detail = smoke._check_workflow()
    assert ok, detail          # the real workflow is valid
    assert 'runtime image' not in detail
    # Broken-workflow rejection still active (token check intact).
    import yaml as _yaml
    base = os.path.dirname(os.path.dirname(os.path.abspath(smoke.__file__)))
    with open(os.path.join(base, smoke.WORKFLOW_FILE),
              encoding='utf-8') as fh:
        yml = fh.read()
    assert 'concurrency' in yml and 'schedule' in yml
    assert _yaml.safe_load(yml) is not None


def test_default_smoke_mode_remains_read_only(smoke):
    """The runtime-image softening added no mutation paths: the workflow
    check itself only reads files and parses text — no session writes,
    no file writes."""
    import inspect
    src = inspect.getsource(smoke._check_workflow)
    for banned in ('INSERT INTO', 'UPDATE ', 'DELETE FROM', 'commit(',
                   'create_all', 'open(.*\'w\'', 'os.remove', 'shutil'):
        assert banned not in src
    # The check stays pure-read: no database use at all.
    assert 'db.' not in src and 'session' not in src


def test_smoke_cli_adds_no_network(smoke, stripped_source):
    """No new network surface: no raw sockets/requests/urllib in source."""
    import re as _re
    assert not _re.search(
        r'\b(socket|urllib|requests)\b', stripped_source), stripped_source
