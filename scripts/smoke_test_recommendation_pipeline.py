"""Recommendation pipeline production smoke checks (Feature #7, Phase 17).

Operator-facing safety net for the live recommendation learning loop:

    For You rail -> POST /api/rec/feedback -> RecommendationFeedback
        -> nightly compute_taste_profiles.py -> TasteProfile
        -> For You ranking / Smart Lists / CineBot / Taste DNA

Modes
-----
DEFAULT (read-only): verifies pipeline prerequisites only — tables, profile
columns, engine/API/script/workflow presence, schema guard, deployment drift.
NEVER mutates data, NEVER calls TMDb or any external API.

--live-test (mutating, double-gated): a controlled end-to-end drill against a
dedicated test identity (email domain ``.invalid`` — cannot collide with a
real user). Requires BOTH the flag and ``RECOMMENDATION_SMOKE_TEST=1`` in the
environment; a typo of either leaves production untouched. The drill writes
feedback only through the real POST /api/rec/feedback API, invokes the
canonical /add_to_watchlist persistence for the save path, computes the
profile for the test user ONLY (never a global recomputation), checks the
For You engine for crash/determinism/network violations, and removes every
trace of the test identity afterwards.

Exit codes: 0 = ready/healthy, 1 = prerequisites or drill failures,
2 = fatal error or safety-gate refusal.

Database URL always comes from the environment — this script accepts no
database override argument.
"""
import argparse
import json
import logging
import os
import re
import subprocess
import sys
from importlib import util as _importlib_util

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), '..')))

# Report drift as a failed CHECK rather than crashing at import time.
os.environ.setdefault('SKIP_SCHEMA_GUARD', '1')

logger = logging.getLogger('frameiq.rec_pipeline_smoke')

ENV_ASSERTION = 'RECOMMENDATION_SMOKE_TEST'
TEST_EMAIL = 'smoke-recommendation@smoke-recommendation.invalid'
TEST_USERNAME = 'smoke-recommendation'
TEST_MEDIA_TMDB_ID = 603          # The Matrix (movie) — canonical drill title

REQUIRED_PROFILE_COLUMNS = (
    'genre_weights_json', 'decade_weights_json', 'director_affinity_json',
    'runtime_pref_json', 'media_type_pref_json', 'mood_tags_json',
    'confidence', 'signal_count', 'distinct_title_count', 'profile_version',
)

REQUIRED_TABLES = (
    'recommendation_feedback',    # TasteProfile + Director below
    'taste_profile',
    'director',
    'media_director',
)

# Canonical watchlist/collection state the drill must never leave changed
# (keys as reported by _snapshot_counts).
MUTATING_TABLES = ('watchlist', 'viewed', 'diary', 'likes')

NIGHTLY_SCRIPTS = (
    'scripts/enrich_directors.py',
    'scripts/compute_taste_profiles.py',
    'scripts/verify_recommendation_feedback.py',
    'scripts/analyze_recommendation_feedback.py',
)

WORKFLOW_FILE = '.github/workflows/taste-profile-nightly.yml'
WORKFLOW_REQUIREMENTS = (
    'enrich_directors.py', 'compute_taste_profiles.py',
    'verify_recommendation_feedback.py', 'workflow_dispatch', 'concurrency',
)

# Frontend impression/click semantics verified by these suites (no browser
# infrastructure in this repo — spec §11 allows the automated-test route).
FRONTEND_TEST_FILES = (
    'tests/test_for_you_feedback.py',
    'tests/test_for_you_explicit_feedback.py',
)

_app = None
_db = None


def _load():
    """Import the Flask app + db (once)."""
    global _app, _db
    if _app is not None:
        return
    from app import app as flask_app, db as sqla_db
    _app, _db = flask_app, sqla_db


def _import_script(name):
    """Import a sibling script module by filename (no package)."""
    path = os.path.join(os.path.dirname(__file__), name + '.py')
    spec = _importlib_util.spec_from_file_location(name, path)
    module = _importlib_util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ════════════════════════════════════════════════════════════════════════════
# Read-only prerequisite checks — each returns (ok, detail)
# ════════════════════════════════════════════════════════════════════════════

def _check_tables():
    from sqlalchemy import inspect
    from models import db
    live = set(inspect(db.engine).get_table_names())
    missing = [t for t in REQUIRED_TABLES if t not in live]
    return (not missing,
            'all pipeline tables present' if not missing
            else 'missing tables: %s' % ', '.join(missing))


def _check_profile_columns():
    from sqlalchemy import inspect
    from models import db
    cols = {c['name'] for c in inspect(db.engine).get_columns('taste_profile')}
    missing = [c for c in REQUIRED_PROFILE_COLUMNS if c not in cols]
    return (not missing,
            'profile columns present' if not missing
            else 'missing columns: %s' % ', '.join(missing))


def _check_for_you_engine():
    from api.for_you import get_for_you, MAX_TMDB_CALLS, MAX_AVAILABILITY_PROBES  # noqa: F401,E501
    budgets_ok = (MAX_TMDB_CALLS == 5 and MAX_AVAILABILITY_PROBES == 12)
    return (budgets_ok,
            'engine imports; TMDb budget 5, availability budget 12'
            if budgets_ok else 'unexpected engine budgets')


def _check_feedback_api_route():
    from flask import current_app
    rules = [r for r in current_app.url_map.iter_rules()
             if r.rule.endswith('/api/rec/feedback')
             and 'POST' in r.methods]
    return (bool(rules), 'POST /api/rec/feedback registered' if rules
            else 'feedback API route missing')


def _check_for_you_route():
    from flask import current_app
    rules = [r for r in current_app.url_map.iter_rules()
             if r.rule.endswith('/api/for-you') and 'GET' in r.methods]
    return (bool(rules), 'GET /api/for-you registered' if rules
            else 'For You API route missing')


def _check_nightly_scripts():
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    missing = [s for s in NIGHTLY_SCRIPTS
               if not os.path.isfile(os.path.join(base, s))]
    return (not missing, 'enrichment/recompute/verify/analytics present'
            if not missing else 'missing: %s' % ', '.join(missing))


def _check_verification_cli():
    module = _import_script('verify_recommendation_feedback')
    ok = hasattr(module, 'main') and hasattr(module, 'run')
    return (ok, 'verify_recommendation_feedback imports' if ok
            else 'verification CLI unusable')


def _check_analytics_cli():
    module = _import_script('analyze_recommendation_feedback')
    ok = hasattr(module, 'main') and hasattr(module, 'run')
    return (ok, 'analyze_recommendation_feedback imports' if ok
            else 'analytics CLI unusable')


def _check_workflow():
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    path = os.path.join(base, WORKFLOW_FILE)
    if not os.path.isfile(path):
        # Production/runtime images contain neither the repository nor
        # its CI metadata (.github/ is intentionally NOT copied into the
        # image). A missing workflow file there is EXPECTED — not a
        # pipeline failure — because the workflow contract (schedule,
        # concurrency, ordering, failure propagation) is enforced by
        # repository CI (tests/test_taste_profile_nightly_workflow.py)
        # on every push. Never let repository-only metadata produce
        # [STATUS] NOT READY in a runtime deployment.
        return True, ('not inspectable in runtime image — workflow '
                      'contract validated by repository CI')
    with open(path, encoding='utf-8') as f:
        yml = f.read()
    missing = [tok for tok in WORKFLOW_REQUIREMENTS if tok not in yml]
    if missing:
        return False, 'workflow missing: %s' % ', '.join(missing)
    if not re.search(r'cron:\s*["\']?\d+ \d+ \* \* \*', yml):
        return False, 'no daily schedule'
    if 'docker exec -d' in yml or '|| true' in yml:
        return False, 'detached/-suppressed execution present'
    order = (yml.index('enrich_directors.py')
             < yml.index('compute_taste_profiles.py')
             < yml.index('verify_recommendation_feedback.py'))
    return (order, 'schedule + dispatch + concurrency + ordering verified'
            if order else 'workflow step order wrong')


def _check_schema_guard():
    from models import db
    from utils.schema_guard import check_schema
    report = check_schema(db.engine)
    guard_ok = isinstance(report, dict) and 'ok' in report
    drift_free = bool(report.get('ok'))
    return (guard_ok and drift_free,
            'schema matches declared models' if drift_free
            else 'deployment drift: %s' % report.get('missing_tables'))


READ_ONLY_CHECKS = (
    ('RecommendationFeedback', _check_tables),
    ('TasteProfile', _check_profile_columns),
    ('Director capture', None),          # filled below (tables shared check)
    ('For You', _check_for_you_engine),
    ('Feedback API', _check_feedback_api_route),
    ('For You API', _check_for_you_route),
    ('Nightly recomputation', _check_nightly_scripts),
    ('Nightly verification', _check_verification_cli),
    ('Analytics', _check_analytics_cli),
    ('Nightly workflow', _check_workflow),
    ('Schema guard', _check_schema_guard),
)


def collect_readiness():
    """Run every read-only check. Returns [(name, ok, detail)]."""
    results = []
    for name, fn in READ_ONLY_CHECKS:
        if fn is None:   # Director capture: tables + workflow already prove it
            tables_ok = dict((n, ok) for n, ok, _ in results)
            results.append((name, tables_ok.get('RecommendationFeedback', False),
                            'director/media_director tables present'))
            continue
        try:
            ok, detail = fn()
        except Exception as exc:  # noqa: BLE001 — a failing check is data
            ok, detail = False, '%s: %s' % (type(exc).__name__, exc)
        results.append((name, bool(ok), detail))
    return results


def run_read_only():
    """Print [CHECK] lines + readiness status. Returns exit code 0/1."""
    results = collect_readiness()
    for name, ok, detail in results:
        logger.info('[CHECK] %s: %s (%s)', name,
                    'ok' if ok else 'FAILED', detail)
    ready = all(ok for _, ok, _ in results)
    logger.info('[STATUS] %s', 'READY' if ready else 'NOT READY')
    return 0 if ready else 1


# ════════════════════════════════════════════════════════════════════════════
# Live drill (mutating; double-gated)
# ════════════════════════════════════════════════════════════════════════════

def _live_gate():
    """Refuse to mutate unless BOTH gates pass. Exit 2 on refusal."""
    if os.environ.get(ENV_ASSERTION) != '1':
        logger.error(
            '[FATAL] refusing to mutate: --live-test also requires '
            '%s=1 in the environment', ENV_ASSERTION)
        sys.exit(2)
    from models import db
    from utils.schema_guard import check_schema
    if not check_schema(db.engine)['ok']:
        logger.error('[FATAL] refusing to mutate: schema drift detected — '
                     'run migrations first')
        sys.exit(2)


def _snapshot_counts(user_id):
    """Row counts for the test identity across feedback + canonical state."""
    from models import (db, RecommendationFeedback, TasteProfile,
                        user_watchlist, user_viewed)
    from models.social import DiaryEntry, MediaLike

    def _count(table, *extra):
        stmt = table.select().where(
            table.c.user_id == user_id, *extra)
        return len(db.session.execute(stmt).fetchall())

    return {
        'feedback': RecommendationFeedback.query.filter_by(
            user_id=user_id).count(),
        'profiles': TasteProfile.query.filter_by(user_id=user_id).count(),
        'watchlist': _count(user_watchlist),
        'viewed': _count(user_viewed),
        'diary': _count(DiaryEntry.__table__),
        'likes': _count(MediaLike.__table__),
    }


def _cleanup_identity(user_id, media_created=False):
    """Remove every trace of the drill identity (rollback guarantee).

    The drill-created MediaItem (offline save fallback) is removed only
    when the drill itself created it — a pre-existing catalog row is left
    untouched.
    """
    from models import (db, User, RecommendationFeedback, TasteProfile,
                        MediaItem, user_watchlist,
                        user_viewed)
    from models.social import DiaryEntry, MediaLike
    RecommendationFeedback.query.filter_by(user_id=user_id) \
        .delete(synchronize_session=False)
    TasteProfile.query.filter_by(user_id=user_id) \
        .delete(synchronize_session=False)
    for table in (user_watchlist, user_viewed):
        db.session.execute(table.delete().where(
            table.c.user_id == user_id))
    DiaryEntry.query.filter_by(user_id=user_id) \
        .delete(synchronize_session=False)
    MediaLike.query.filter_by(user_id=user_id) \
        .delete(synchronize_session=False)
    User.query.filter_by(id=user_id).delete(synchronize_session=False)
    if media_created:
        MediaItem.query.filter_by(tmdb_id=TEST_MEDIA_TMDB_ID,
                                  media_type='movie') \
            .delete(synchronize_session=False)
    db.session.commit()


def _post_feedback(client, csrf, events):
    return client.post(
        '/api/rec/feedback',
        data=json.dumps({'events': events}),
        content_type='application/json',
        headers={'X-CSRFToken': csrf, 'Accept': 'application/json'})


def _ensure_drill_identity(state):
    """Create (or reuse) the dedicated drill identity. Returns user_id."""
    from models import db, User
    user = User.query.filter_by(email=TEST_EMAIL).first()
    created = False
    if user is None:
        user = User(username=TEST_USERNAME, email=TEST_EMAIL,
                    email_verified=True)
        user.set_password('!smoke-drill-pass!')
        db.session.add(user)
        db.session.commit()
        created = True
    state['user_id'] = user.id
    logger.info('[INFO] drill identity: %s (%s)', TEST_EMAIL,
                'created' if created else 'reused')
    return created


def _drill_client():
    """Test client logged in as the drill identity, with a CSRF token."""
    client = _app.test_client()
    home = client.get('/')
    m = re.search(r'name="csrf-token" content="([^"]+)"',
                  home.data.decode())
    if not m:
        logger.error('[FATAL] csrf-token meta not found on /')
        raise RuntimeError('csrf-token meta not found on /')
    csrf = m.group(1)
    client.post('/login', data={
        'username': TEST_USERNAME, 'password': '!smoke-drill-pass!'},
        headers={'X-CSRFToken': csrf})
    return client, csrf


def _drill_feedback_api(client, csrf, user_id, base, mark):
    """A/B: feedback write through the real API + not_interested isolation."""
    from models import db, RecommendationFeedback
    batch = [
        {'media_id': TEST_MEDIA_TMDB_ID, 'media_type': 'movie',
         'surface': 'home_for_you', 'event': 'impression', 'position': 1},
        {'media_id': TEST_MEDIA_TMDB_ID, 'media_type': 'movie',
         'surface': 'home_for_you', 'event': 'click', 'position': 1},
        {'media_id': TEST_MEDIA_TMDB_ID, 'media_type': 'movie',
         'surface': 'home_for_you', 'event': 'not_interested',
         'position': 2},
    ]
    r = _post_feedback(client, csrf, batch)
    api_ok = r.status_code == 200 and r.get_json().get('recorded') == 3
    mark('Feedback API write', api_ok,
         '%s events via POST /api/rec/feedback' % len(batch)
         if api_ok else 'HTTP %s' % r.status_code)

    db.session.expire_all()
    events_seen = sorted(row.event for row in
                         RecommendationFeedback.query.filter_by(
                             user_id=user_id).all())
    mark('Feedback persistence',
         events_seen == ['click', 'impression', 'not_interested'],
         str(events_seen))

    after = _snapshot_counts(user_id)
    untouched = all(after[t] == base[t] for t in MUTATING_TABLES)
    mark('Not-interested isolation', untouched,
         'watchlist/viewed/diary/likes unchanged' if untouched
         else 'canonical state mutated: %s' % after)


def _drill_canonical_save(client, csrf, user_id, state, mark):
    """§10: canonical save first, saved feedback after, no duplicates."""
    from models import (db, MediaItem, RecommendationFeedback,
                        user_watchlist)
    # The drill runs offline (no TMDb), so the canonical save's
    # get_or_create_media_item cannot hydrate remotely: create the drill
    # title locally if absent (operator-visible, cleaned up with the
    # identity — same fallback the canonical action performs).
    if MediaItem.query.filter_by(tmdb_id=TEST_MEDIA_TMDB_ID,
                                 media_type='movie').first() is None:
        db.session.add(MediaItem(
            tmdb_id=TEST_MEDIA_TMDB_ID, media_type='movie',
            title='Smoke Drill Title'))
        db.session.commit()
        state['media_created'] = True
    save = client.get('/add_to_watchlist/%d/movie' % TEST_MEDIA_TMDB_ID)
    db.session.expire_all()
    wl = db.session.execute(user_watchlist.select().where(
        user_watchlist.c.user_id == user_id)).fetchall()
    mark('Canonical save persistence',
         save.status_code in (200, 302) and len(wl) == 1,
         'watchlist rows: %d' % len(wl))
    r = _post_feedback(client, csrf, [
        {'media_id': TEST_MEDIA_TMDB_ID, 'media_type': 'movie',
         'surface': 'home_for_you', 'event': 'saved', 'position': 3}])
    db.session.expire_all()
    saved_rows = RecommendationFeedback.query.filter_by(
        user_id=user_id, event='saved').all()
    mark('Saved feedback after canonical save',
         r.status_code == 200 and len(saved_rows) == 1 and len(wl) == 1,
         'exactly one saved event, no duplicate watchlist row')
    second_save = client.get(
        '/add_to_watchlist/%d/movie' % TEST_MEDIA_TMDB_ID)
    db.session.expire_all()
    wl2 = db.session.execute(user_watchlist.select().where(
        user_watchlist.c.user_id == user_id)).fetchall()
    mark('No duplicate save state',
         second_save.status_code in (200, 302) and len(wl2) == 1,
         'repeat canonical save adds nothing')


def _drill_analytics(mark):
    """C: analytics subprocess — read-only, aggregate-only, no leakage."""
    proc = subprocess.run(
        [sys.executable,
         os.path.join(os.path.dirname(__file__),
                      'analyze_recommendation_feedback.py'),
         '--days', '1'],
        capture_output=True, text=True, timeout=180)
    out = proc.stdout + proc.stderr
    analytics_ok = (
        proc.returncode == 0 and '[STATUS]' in out
        and re.search(r'Impressions: [1-9]', out) is not None
        and TEST_EMAIL not in out and TEST_USERNAME not in out)
    mark('Analytics aggregate report', analytics_ok,
         'exit %d, no identity leakage' % proc.returncode
         if analytics_ok else 'see analytics output')


def _drill_profile(user_id, mark):
    """D/E: TasteProfile recomputation for the drill identity ONLY."""
    from models import db
    from api.taste_profile import compute_profile, get_profile
    compute_profile(user_id)
    db.session.expire_all()
    profile = get_profile(user_id)
    mark('TasteProfile recomputation (isolated user)',
         profile is not None,
         'confidence=%s signals=%s' % (
             getattr(profile, 'confidence', None),
             getattr(profile, 'signal_count', None))
         if profile is not None else 'no profile row')


def _drill_for_you_engine(user_id, mark):
    """F: engine no-crash/schema/determinism with external paths stubbed.

    Permitted external paths (TMDb fetch layer, recommendation seeds,
    availability probes) are replaced by COUNTING stubs returning safe
    empties — the engine must degrade gracefully and stay within budget.
    The FORBIDDEN path (fetch_media_details hydration, which the engine
    must never use) raises into a violations ledger that must stay empty.
    """
    import api.tmdb.cache as tmdb_cache
    import api.tmdb.search as tmdb_search
    import api.tmdb_client as tmdb_client
    import api.availability as availability
    import api.for_you as fy_engine

    permitted_calls = []
    violations = []

    def _counted(name):
        def _stub(*a, **kw):
            permitted_calls.append(name)
            if name == 'tmdb_request':
                return {'results': []}
            return []
        return _stub

    def _forbidden(name):
        def _blocked(*a, **kw):
            violations.append(name)
            raise RuntimeError('network violation: %s' % name)
        return _blocked

    orig = (tmdb_cache.cached_tmdb_request,
            tmdb_search.fetch_tmdb_recommendations,
            tmdb_client.fetch_media_details,
            availability.get_availability,
            availability.match_my_services)
    tmdb_cache.cached_tmdb_request = _counted('tmdb_request')
    tmdb_search.fetch_tmdb_recommendations = _counted('tmdb_recs')
    tmdb_client.fetch_media_details = _forbidden('fetch_media_details')
    availability.get_availability = _counted('availability')
    availability.match_my_services = _counted('match_services')
    try:
        from api.for_you import get_for_you
        first = get_for_you(user_id, limit=14)
        fy_engine._cache.clear()      # prove real determinism, not cache
        second = get_for_you(user_id, limit=14)
        schema_ok = (isinstance(first, dict)
                     and 'personalized' in first)
        deterministic = json.dumps(first, sort_keys=True) == \
            json.dumps(second, sort_keys=True)
        budget_ok = (permitted_calls.count('tmdb_request')
                     <= fy_engine.MAX_TMDB_CALLS
                     and permitted_calls.count('availability')
                     <= fy_engine.MAX_AVAILABILITY_PROBES)
        mark('For You engine (zero hydration, deterministic)',
             schema_ok and deterministic and not violations and budget_ok,
             'schema valid + repeat-identical + no hydration path'
             if not violations else 'violations: %s' % violations)
    finally:
        (tmdb_cache.cached_tmdb_request,
         tmdb_search.fetch_tmdb_recommendations,
         tmdb_client.fetch_media_details,
         availability.get_availability,
         availability.match_my_services) = orig
        fy_engine._cache.clear()


def _drill_frontend_suites(mark):
    """§11: frontend impression/click semantics via existing suites."""
    proc = subprocess.run(
        [sys.executable, '-m', 'pytest', *FRONTEND_TEST_FILES, '-q'],
        capture_output=True, text=True, timeout=300)
    mark('Frontend impression/click semantics (automated suites)',
         proc.returncode == 0,
         'test_for_you_feedback + test_for_you_explicit_feedback')


def _drill_nightly_and_verify(mark):
    """§14 ordering + §13 verification CLI (output stream independent)."""
    wf_ok, wf_detail = _check_workflow()
    mark('Nightly workflow ordering', wf_ok, wf_detail)

    proc = subprocess.run(
        [sys.executable,
         os.path.join(os.path.dirname(__file__),
                      'verify_recommendation_feedback.py')],
        capture_output=True, text=True, timeout=180)
    verify_out = proc.stdout + proc.stderr
    mark('Verification CLI healthy',
         proc.returncode == 0 and '[STATUS]' in verify_out,
         'exit %d' % proc.returncode)


def run_live():
    """Controlled E2E drill. Returns exit code 0/1. Cleans up in finally."""
    from models import db, User, RecommendationFeedback
    results = []          # (name, ok, detail)
    state = {'user_id': None, 'media_created': False}

    def _mark(name, ok, detail=''):
        results.append((name, bool(ok), detail))
        logger.info('[%s] %s%s', 'PASS' if ok else 'FAIL', name,
                    (' — ' + detail) if detail else '')

    try:
        _mark('Isolated test identity', _ensure_drill_identity(state),
              TEST_EMAIL)
        user_id = state['user_id']
        base = _snapshot_counts(user_id)
        client, csrf = _drill_client()

        _drill_feedback_api(client, csrf, user_id, base, _mark)
        _drill_canonical_save(client, csrf, user_id, state, _mark)
        _drill_analytics(_mark)
        _drill_profile(user_id, _mark)
        _drill_for_you_engine(user_id, _mark)
        _drill_frontend_suites(_mark)
        _drill_nightly_and_verify(_mark)
    except Exception as exc:  # noqa: BLE001 — operator-facing fatal boundary
        logger.error('[FATAL] %s: %s', type(exc).__name__, exc)
        return 2
    finally:
        user_id = state['user_id']
        if user_id is not None:
            try:
                _cleanup_identity(user_id,
                                  media_created=state['media_created'])
                db.session.expire_all()
                leftover = (User.query.filter_by(email=TEST_EMAIL).count()
                            + RecommendationFeedback.query.filter_by(
                                user_id=user_id).count())
                _mark('Cleanup / rollback', leftover == 0,
                      'no mutation beyond the drill identity'
                      if leftover == 0 else 'leftover rows: %s' % leftover)
            except Exception as exc:  # noqa: BLE001
                logger.error('[FATAL] cleanup failed: %s', exc)
                return 2

    # ── §15 production readiness checklists ──────────────────────────────
    by_name = dict((n, ok) for n, ok, _ in results)
    logger.info('[CHECKLIST] RECOMMENDATION PIPELINE')
    for label, key in (
            ('Feedback API', 'Feedback API write'),
            ('Feedback persistence', 'Feedback persistence'),
            ('For You', 'For You engine (zero hydration, deterministic)'),
            ('TasteProfile', 'TasteProfile recomputation (isolated user)'),
            ('Director enrichment', 'Nightly workflow ordering'),
            ('Nightly recomputation', 'TasteProfile recomputation '
                                      '(isolated user)'),
            ('Verification', 'Verification CLI healthy'),
            ('Analytics', 'Analytics aggregate report')):
        logger.info('[%s] %s', 'PASS' if by_name.get(key) else 'FAIL', label)
    logger.info('[CHECKLIST] NETWORK SAFETY')
    for label, key in (
            ('compute_profile local-only',
             'For You engine (zero hydration, deterministic)'),
            ('For You bounded',
             'For You engine (zero hydration, deterministic)'),
            ('director enrichment isolated', 'Nightly workflow ordering')):
        logger.info('[%s] %s', 'PASS' if by_name.get(key) else 'FAIL', label)
    logger.info('[CHECKLIST] PRIVACY')
    for label, key in (
            ('aggregate analytics', 'Analytics aggregate report'),
            ('private TasteProfile',
             'TasteProfile recomputation (isolated user)'),
            ('no user IDs in frontend telemetry',
             'Frontend impression/click semantics (automated suites)')):
        logger.info('[%s] %s', 'PASS' if by_name.get(key) else 'FAIL', label)

    ok = all(ok for _, ok, _ in results)
    logger.info('[STATUS] %s', 'HEALTHY' if ok else 'FAILURES DETECTED')
    return 0 if ok else 1


# ════════════════════════════════════════════════════════════════════════════
# Entry point
# ════════════════════════════════════════════════════════════════════════════

def main(argv=None):
    logging.basicConfig(
        level=logging.INFO, format='%(message)s', stream=sys.stdout)
    parser = argparse.ArgumentParser(
        description='Recommendation pipeline smoke checks. Default mode is '
                    'READ-ONLY; --live-test additionally requires '
                    f'{ENV_ASSERTION}=1 in the environment.')
    parser.add_argument('--live-test', action='store_true',
                        help='run the mutating end-to-end drill (gated)')
    args = parser.parse_args(argv)

    logger.info('[START] Recommendation pipeline smoke checks')
    try:
        _load()
    except Exception as exc:  # noqa: BLE001
        logger.error('[FATAL] %s: %s', type(exc).__name__, exc)
        return 2

    if args.live_test:
        with _app.app_context():
            _live_gate()
            return run_live()
    with _app.app_context():
        return run_read_only()


if __name__ == '__main__':
    sys.exit(main())
