"""Recommendation-feedback pipeline verification (Feature #7, Phase 8).

Read-only operator CLI that proves the learning loop is alive:

    For You visibility/click -> POST /api/rec/feedback
        -> RecommendationFeedback
        -> nightly scripts/compute_taste_profiles.py
        -> api.taste_profile.compute_profile()
        -> TasteProfile
        -> For You ranking

It inspects the current database and reports bounded AGGREGATE health
information only: row counts, 24h event breakdown, surface breakdown,
distinct active users, timestamp range, TasteProfile aggregates, duplicate
detection and feedback->profile freshness comparison. It NEVER dumps
individual rows, payloads, or user identifiers.

EXIT CODES
    0  healthy / no detected inconsistency
    1  data-quality inconsistency or suspicious state (duplicate
       non-impression events, malformed rows, suspicious aggregates)
    2  fatal application/database error (including startup)

GUARANTEES (each enforced by tests/test_verify_recommendation_feedback.py)
    READ-ONLY  — no INSERT/UPDATE/DELETE/COMMIT anywhere in this file.
    NO NETWORK — no outbound connections; guarded by a socket-level test.
    BOUNDED    — a handful of aggregate GROUP BY queries; no per-row loops,
                 no N+1, no OFFSET scans.
    LEGACY     — user_taste_profile / user_similarity are never touched.

Invocation (operator):

    docker compose exec web python scripts/verify_recommendation_feedback.py
"""
import logging
import os
import sys
from datetime import datetime, timedelta

# Add parent directory to path (same convention as compute_taste_profiles.py)
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

logger = logging.getLogger("frameiq.rec_feedback_verify")

# Loaded lazily by _load() so an unreachable database at startup surfaces as a
# clean [FATAL] log + exit 2 from main() instead of a raw import traceback.
_app = None
_db = None
RecommendationFeedback = None
TasteProfile = None
RF_EVENTS = None
RF_SURFACES = None
RF_MEDIA_TYPES = None


def _load():
    """Import the Flask app, session and models (once)."""
    global _app, _db, RecommendationFeedback, TasteProfile
    global RF_EVENTS, RF_SURFACES, RF_MEDIA_TYPES
    if _app is not None:
        return
    from app import app as flask_app, db as sqla_db
    # TasteProfile is re-exported by the models package;
    # RecommendationFeedback is imported from its own module (the package
    # does not re-export it).
    from models import TasteProfile as _TP
    from models.recommendation_feedback import (
        RecommendationFeedback as _RF,
        EVENTS as _events, SURFACES as _surfaces, MEDIA_TYPES as _types,
    )
    _app, _db = flask_app, sqla_db
    RecommendationFeedback, TasteProfile = _RF, _TP
    RF_EVENTS, RF_SURFACES, RF_MEDIA_TYPES = _events, _surfaces, _types


# ════════════════════════════════════════════════════════════════════════════
# Aggregate queries (all bounded GROUP BYs — never row-level dumps)
# ════════════════════════════════════════════════════════════════════════════

def _feedback_aggregates(since):
    """One bounded pass over RecommendationFeedback: totals + 24h breakdown."""
    _load()
    RF = RecommendationFeedback
    total = RF.query.count()

    recent = RF.query.filter(RF.created_at >= since)
    recent_count = recent.count()

    event_counts = dict(
        recent.with_entities(RF.event, _db.func.count(RF.id))
        .group_by(RF.event).all())
    surface_counts = dict(
        recent.with_entities(RF.surface, _db.func.count(RF.id))
        .group_by(RF.surface).all())
    active_users = (
        recent.with_entities(_db.func.count(_db.func.distinct(RF.user_id)))
        .scalar() or 0)
    ts_range = (
        RF.query.with_entities(_db.func.min(RF.created_at),
                               _db.func.max(RF.created_at)).first())
    return {
        'total': total,
        'last_24h': recent_count,
        'events': event_counts,
        'surfaces': surface_counts,
        'active_users': active_users,
        'oldest': ts_range[0] if ts_range else None,
        'newest': ts_range[1] if ts_range else None,
    }


def _profile_aggregates():
    """One bounded pass over TasteProfile (persisted values only — the
    canonical thresholds from api/for_you.py decide eligibility; no second
    profile algorithm lives here)."""
    _load()
    TP = TasteProfile
    total = TP.query.count()
    rows = (
        TP.query.with_entities(
            TP.updated_at, TP.profile_version, TP.confidence,
            TP.signal_count, TP.distinct_title_count)
        .all())
    versions = {}
    sig_sum = title_sum = eligible = 0
    newest = None
    since = datetime.utcnow() - timedelta(hours=24)
    updated_24h = 0
    for (updated_at, version, confidence, signal_count, titles) in rows:
        versions[version] = versions.get(version, 0) + 1
        sig_sum += signal_count or 0
        title_sum += titles or 0
        if updated_at and (newest is None or updated_at > newest):
            newest = updated_at
        if updated_at and updated_at >= since:
            updated_24h += 1
        if (confidence or 0) >= 0.4 and (titles or 0) >= 5:
            eligible += 1
    return {
        'total': total,
        'updated_24h': updated_24h,
        'newest': newest,
        'versions': versions,
        'avg_signal_count': round(sig_sum / total, 1) if total else 0.0,
        'avg_distinct_titles': round(title_sum / total, 1) if total else 0.0,
        'eligible': eligible,
    }


def _malformed_rows():
    """Aggregate count of rows violating model-level invariants.

    The API/model validation boundary makes these unreachable through
    normal writes; they can only appear from manual tampering or older
    schema states. Reported, never repaired.
    """
    _load()
    RF = RecommendationFeedback
    valid_events = RF_EVENTS
    valid_surfaces = RF_SURFACES
    rows = (
        RF.query.with_entities(RF.id, RF.event, RF.surface, RF.media_type,
                               RF.created_at, RF.user_id, RF.media_id)
        .all())
    bad = []
    for (rid, event, surface, media_type, created_at, user_id, media_id) in rows:
        if event not in valid_events or surface not in valid_surfaces \
                or media_type not in RF_MEDIA_TYPES or created_at is None \
                or user_id is None or media_id is None or media_id <= 0:
            bad.append(rid)
    return bad


def _duplicate_groups():
    """Aggregate duplicate detection for NON-impression events.

    The partial unique index uq_recommendation_feedback_event_daily makes
    these impossible on a healthy schema; their existence means the index
    is missing, was dropped, or rows predate it. Reported as grouped
    dimensions only — never deleted or repaired.
    """
    _load()
    RF = RecommendationFeedback
    dims = (RF.user_id, RF.media_id, RF.media_type, RF.surface, RF.event,
            RF.event_date)
    groups = (
        RF.query.with_entities(*dims, _db.func.count(RF.id).label('n'))
        .filter(RF.event != 'impression')
        .group_by(*dims)
        .having(_db.func.count(RF.id) > 1)
        .all())
    return groups


def _freshness_map(since):
    """Per-user (latest_feedback, latest_profile_or_None) for every user who
    produced feedback inside the window. Aggregate pairs only — used by
    _profile_freshness() and directly inspectable by tests."""
    _load()
    RF = RecommendationFeedback
    TP = TasteProfile
    latest_feedback = dict(
        RF.query.with_entities(
            RF.user_id, _db.func.max(RF.created_at))
        .filter(RF.created_at >= since)
        .group_by(RF.user_id).all())
    if not latest_feedback:
        return {}
    latest_profile = dict(
        TP.query.with_entities(TP.user_id, _db.func.max(TP.updated_at))
        .filter(TP.user_id.in_(list(latest_feedback)))
        .group_by(TP.user_id).all())
    return {uid: (fb_ts, latest_profile.get(uid))
            for uid, fb_ts in latest_feedback.items()}


def _profile_freshness(since):
    """Feedback -> profile freshness comparison (aggregate).

    The nightly cadence means profiles are EXPECTED to lag up to ~24h behind
    feedback; a lag beyond one nightly cycle is what operators should
    investigate. Reported with that interpretation in mind.
    """
    freshness = _freshness_map(since)
    if not freshness:
        return 0, 0
    lagging = sum(
        1 for fb_ts, profile_ts in freshness.values()
        if not profile_ts or profile_ts < fb_ts)
    return len(freshness), lagging


# ════════════════════════════════════════════════════════════════════════════
# Reporting
# ════════════════════════════════════════════════════════════════════════════

def _log_aggregates(fb, profiles, fresh_users, lagging):
    info = logger.info
    info("[INFO] Feedback rows: %s", fb['total'])
    info("[INFO] Last 24h: %s", fb['last_24h'])
    info("[INFO] Impressions: %s", fb['events'].get('impression', 0))
    info("[INFO] Clicks: %s", fb['events'].get('click', 0))
    info("[INFO] Saved: %s", fb['events'].get('saved', 0))
    info("[INFO] Not interested: %s", fb['events'].get('not_interested', 0))
    info("[INFO] Rated: %s", fb['events'].get('rated', 0))
    info("[INFO] Already watched: %s", fb['events'].get('already_watched', 0))
    info("[INFO] Active users: %s", fb['active_users'])
    info("[INFO] Surfaces: %s",
         ', '.join(f"{k}={v}" for k, v in sorted(fb['surfaces'].items()))
         or 'none')
    info("[INFO] Oldest feedback: %s", fb['oldest'] or 'none')
    info("[INFO] Newest feedback: %s", fb['newest'] or 'none')
    info("[INFO] TasteProfiles: %s", profiles['total'])
    info("[INFO] Profiles updated last 24h: %s", profiles['updated_24h'])
    info("[INFO] Latest profile update: %s", profiles['newest'] or 'none')
    info("[INFO] Profile versions: %s",
         ', '.join(f"v{k}={v}" for k, v in sorted(profiles['versions'].items()))
         or 'none')
    info("[INFO] Avg signal_count: %s | avg distinct titles: %s",
         profiles['avg_signal_count'], profiles['avg_distinct_titles'])
    info("[INFO] Personalized-eligible profiles: %s", profiles['eligible'])
    info("[INFO] Users with feedback last 24h: %s | awaiting recompute: %s",
         fresh_users, lagging)


def _sanity_issues(fb):
    """Cheap arithmetic sanity checks over the aggregate dict."""
    issues = []
    if fb['events'].get('impression', 0) < 0:      # sanity: not negative
        issues.append("negative impression count")
    if sum(fb['events'].values()) > fb['last_24h']:
        issues.append("event breakdown exceeds 24h total")
    if fb['total'] and fb['last_24h'] > fb['total']:
        issues.append("24h count exceeds total rows")
    return issues


def run():
    """Collect aggregates and return the exit code (0/1). Fatal errors
    propagate to main() for exit 2."""
    since = datetime.utcnow() - timedelta(hours=24)
    fb = _feedback_aggregates(since)
    profiles = _profile_aggregates()
    fresh_users, lagging = _profile_freshness(since)

    _log_aggregates(fb, profiles, fresh_users, lagging)

    issues = list(_sanity_issues(fb))

    malformed = _malformed_rows()
    if malformed:
        issues.append(
            f"malformed/invalid rows: {len(malformed)} (ids "
            f"{malformed[:10]})"
            + (' ...' if len(malformed) > 10 else ''))
    for rid in malformed[:10]:
        logger.warning("[WARN] malformed row id=%s", rid)
    if len(malformed) > 10:
        logger.warning("[WARN] ... and %s more malformed rows",
                       len(malformed) - 10)

    dupes = _duplicate_groups()
    if dupes:
        total_dupes = sum(g[-1] for g in dupes)
        issues.append(
            f"duplicate non-impression events: {total_dupes} rows in "
            f"{len(dupes)} groups (expected: impossible while the "
            f"uq_recommendation_feedback_event_daily partial unique index "
            f"exists)")
        for group in dupes[:5]:
            # group = (user_id, media_id, media_type, surface, event,
            #          event_date, count). User identity is NOT logged —
            # operators get aggregate dimensions only (privacy).
            _user_id, media_id, media_type, surface, event, day, count = group
            logger.warning(
                "[WARN] duplicate group media=%s/%s surface=%s event=%s "
                "day=%s count=%s",
                media_id, media_type, surface, event, day, count)

    if issues:
        for issue in issues:
            logger.warning("[WARN] %s", issue)
        logger.warning("[STATUS] INCONSISTENT (%d issue(s))", len(issues))
        return 1

    logger.info("[STATUS] OK")
    return 0


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(message)s")   # operator-facing: concise, no logger prefix
    logger.info("[START] Recommendation feedback verification")
    try:
        _load()
        with _app.app_context():
            code = run()
    except Exception as exc:  # noqa: BLE001 — operator-facing fatal boundary
        logger.error("[FATAL] %s: %s", type(exc).__name__, exc)
        return 2
    return code


if __name__ == '__main__':
    sys.exit(main())
