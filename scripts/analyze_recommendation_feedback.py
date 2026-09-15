"""Recommendation feedback analytics (Feature #7, Phase 15).

Read-only operator CLI that measures whether the For You recommendation
system is actually used and producing useful signals:

    For You visibility/click -> POST /api/rec/feedback
        -> RecommendationFeedback  <-- THIS SCRIPT OBSERVES HERE (read-only)
        -> nightly compute_profile() -> TasteProfile -> For You ranking

It reports bounded AGGREGATE metrics for a configurable window (default
7 days, hard bounds 1–90): event totals, CTR, active users, distinct
titles, per-surface / per-media-type / per-position (1–14) / per-reason /
per-source breakdowns, daily trends, descriptive event funnels and
data-quality checks. It NEVER dumps rows, payloads, or user identifiers,
and NEVER mutates anything.

EXIT CODES
    0  analytics completed, no data-quality inconsistency
    1  data-quality inconsistency detected (malformed rows, duplicates)
    2  fatal application/database error (including startup and bad CLI)

GUARANTEES (each enforced by tests/test_analyze_recommendation_feedback.py)
    READ-ONLY  — no INSERT/UPDATE/DELETE/COMMIT anywhere in this file.
    NO NETWORK — no outbound connections; guarded by a socket-level test.
    BOUNDED    — <= 12 aggregate SQL statements per run regardless of row
                 count; output bounded (reasons/sources top 10, positions
                 1–14, daily rows <= 90).
    PRIVATE    — aggregate only; no user IDs, usernames, emails, payloads.
    LEGACY     — user_taste_profile / user_similarity are never touched.

Invocation (operator):

    python scripts/analyze_recommendation_feedback.py
    python scripts/analyze_recommendation_feedback.py --days 30
"""
import argparse
import logging
import os
import sys
from datetime import datetime, timedelta

# Add parent directory to path (same convention as compute_taste_profiles.py)
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

logger = logging.getLogger("frameiq.rec_feedback_analytics")

# Window bounds (spec §3): bounded CLI, no unbounded historical analytics.
MIN_DAYS = 1
MAX_DAYS = 90
DEFAULT_DAYS = 7

# Output bounds (spec §23).
TOP_REASONS = 10
TOP_SOURCES = 10
MAX_POSITION = 14          # spec §6/§13: positions 1–14 individually
MAX_DAILY_ROWS = MAX_DAYS  # daily rows <= 90

# Position bucket for unexpected values (spec §6).
POSITION_OUT_OF_RANGE = 'out_of_range'

# Loaded lazily by _load() so an unreachable database at startup surfaces as a
# clean [FATAL] log + exit 2 from main() instead of a raw import traceback.
_app = None
_db = None
RecommendationFeedback = None
RF_EVENTS = None
RF_SURFACES = None
RF_MEDIA_TYPES = None

_model = None  # loaded by _load_model() for quality checks


def _load():
    """Import the Flask app, session and models (once)."""
    global _app, _db, RecommendationFeedback, RF_EVENTS, RF_SURFACES
    global RF_MEDIA_TYPES, _model
    if _app is not None:
        return
    from app import app as flask_app, db as sqla_db
    from models.recommendation_feedback import (
        RecommendationFeedback as _RF,
        EVENTS as _events, SURFACES as _surfaces, MEDIA_TYPES as _types,
    )
    _app, _db = flask_app, sqla_db
    RecommendationFeedback, RF_EVENTS, RF_SURFACES = _RF, _events, _surfaces
    RF_MEDIA_TYPES = _types
    _model = _RF  # quality checks reuse the same model constants


# ════════════════════════════════════════════════════════════════════════════
# Pure metric helpers (deterministic, unit-testable)
# ════════════════════════════════════════════════════════════════════════════

def _ctr(numerator, denominator):
    """Deterministic percentage: clicks / impressions.

    Zero denominator -> 'n/a' (never divide by zero, spec §2/§12).
    """
    if not denominator:
        return 'n/a'
    return f'{(numerator / denominator) * 100:.2f}%'


def _bounded_position(position):
    """Bucket a rendered position: 1..14 individually, anything else into
    the out-of-range bucket (None -> None: no position recorded)."""
    if position is None:
        return None
    if 1 <= position <= MAX_POSITION:
        return position
    return POSITION_OUT_OF_RANGE


def _window_dates(days):
    """The calendar days (UTC) covered by the window, oldest first — used
    to render a stable daily table with zero-filled missing days."""
    today = datetime.utcnow().date()
    return [today - timedelta(days=days - 1 - i) for i in range(days)]


# ════════════════════════════════════════════════════════════════════════════
# Aggregate queries (SQL GROUP BY only — never row-level dumps; <= 12 total)
# ════════════════════════════════════════════════════════════════════════════

def _count_case(column_or_predicate, value=None):
    """Portable conditional-count aggregate (SQLite + PostgreSQL).

    Two forms:

      _count_case(RF.event, 'impression')   -> counts event = 'impression'
      _count_case(RF.created_at.is_(None))  -> counts a boolean predicate

    Boolean predicates MUST be passed directly. Wrapping them as
    ``predicate == 1`` renders ``(boolean) = 1`` in SQL, which PostgreSQL
    rejects with ``operator does not exist: boolean = integer`` (SQLite
    silently accepts it — the reason production failed while tests
    passed). CASE takes the predicate as-is: case((pred, 1), else_=0).
    """
    predicate = (column_or_predicate == value if value is not None
                 else column_or_predicate)
    return _db.func.sum(_db.case((predicate, 1), else_=0))


def _quality_entities():
    """Data-quality conditional counts (statement 10's select entities).

    Every expression is a boolean predicate handed to CASE directly —
    invalid event/surface/media_type (NOT IN), invalid media_id
    (<= 0 OR NULL), and NULL created_at — so the compiled SQL is
    type-correct on PostgreSQL as well as SQLite.
    """
    RF = RecommendationFeedback
    return (
        _count_case(~RF.event.in_(RF_EVENTS)).label('bad_event'),
        _count_case(~RF.surface.in_(RF_SURFACES)).label('bad_surface'),
        _count_case(~RF.media_type.in_(RF_MEDIA_TYPES))
        .label('bad_media_type'),
        _count_case((RF.media_id <= 0) | (RF.media_id.is_(None)))
        .label('bad_media_id'),
        _count_case(RF.created_at.is_(None)).label('null_created_at'),
    )


def collect(days):
    """Gather every aggregate for the window in <= 12 bounded statements.

    Returns a plain dict; no user-level rows ever leave the DB layer
    (the per-user query is aggregated again in Python before use).
    """
    _load()
    RF = RecommendationFeedback
    since = datetime.utcnow() - timedelta(days=days)
    agg = {'days': days}

    # 1 — event totals for the window
    window = RF.query.filter(RF.created_at >= since)
    agg['events'] = dict(
        window.with_entities(RF.event, _db.func.count(RF.id))
        .group_by(RF.event).all())
    agg['total_events'] = sum(agg['events'].values())

    # 2 — surface x event counts
    surface_rows = (
        window.with_entities(RF.surface, RF.event, _db.func.count(RF.id))
        .group_by(RF.surface, RF.event).all())
    agg['surfaces'] = {}
    for surface, event, count in surface_rows:
        agg['surfaces'].setdefault(surface, {})[event] = count

    # 3 — media_type x event counts
    media_rows = (
        window.with_entities(RF.media_type, RF.event, _db.func.count(RF.id))
        .group_by(RF.media_type, RF.event).all())
    agg['media'] = {}
    for media_type, event, count in media_rows:
        agg['media'].setdefault(media_type, {})[event] = count

    # 4 — position x event counts (impressions + clicks only)
    position_rows = (
        window.with_entities(RF.position, RF.event, _db.func.count(RF.id))
        .filter(RF.event.in_(('impression', 'click')))
        .group_by(RF.position, RF.event).all())
    agg['positions'] = {}
    for position, event, count in position_rows:
        bucket = _bounded_position(position)
        if bucket is None:
            continue  # no position recorded — not a position datum
        agg['positions'].setdefault(bucket, {})[event] = count

    # 5 — reason_kind x event counts (impressions + clicks only)
    reason_rows = (
        window.with_entities(RF.reason_kind, RF.event, _db.func.count(RF.id))
        .filter(RF.event.in_(('impression', 'click')),
                RF.reason_kind.isnot(None))
        .group_by(RF.reason_kind, RF.event).all())
    agg['reasons'] = {}
    for reason, event, count in reason_rows:
        agg['reasons'].setdefault(reason, {})[event] = count

    # 6 — source x event counts (impressions + clicks only)
    source_rows = (
        window.with_entities(RF.source, RF.event, _db.func.count(RF.id))
        .filter(RF.event.in_(('impression', 'click')))
        .group_by(RF.source, RF.event).all())
    agg['sources'] = {}
    for source, event, count in source_rows:
        agg['sources'].setdefault(source, {})[event] = count

    # 7 — daily trend by calendar day (event_date is the UTC day column)
    daily_rows = (
        window.with_entities(RF.event_date, RF.event, _db.func.count(RF.id))
        .group_by(RF.event_date, RF.event).all())
    agg['daily'] = {}
    for day, event, count in daily_rows:
        agg['daily'].setdefault(day, {})[event] = count

    # 8 — per-user impression/click aggregates (re-aggregated in Python;
    #     user identity never reaches the report)
    user_rows = (
        window.with_entities(
            RF.user_id,
            _count_case(RF.event, 'impression').label('impressions'),
            _count_case(RF.event, 'click').label('clicks'))
        .group_by(RF.user_id).all())
    agg['users_with_impressions'] = sum(1 for _, imp, _ in user_rows
                                        if imp and imp > 0)
    agg['users_with_clicks'] = sum(1 for _, _, clk in user_rows
                                   if clk and clk > 0)
    agg['users_with_both'] = sum(
        1 for _, imp, clk in user_rows if imp and imp > 0 and clk and clk > 0)
    agg['active_users'] = len(user_rows)

    # 9 — distinct recommended titles (media identity, not titles' text)
    agg['distinct_titles'] = (
        window.with_entities(_db.func.count(_db.func.distinct(
            _db.func.concat(RF.media_type, ':', RF.media_id))))
        .scalar() or 0)

    # 10 — data-quality counts (one statement, conditional aggregates)
    quality = RF.query.with_entities(*_quality_entities()).first()
    agg['quality'] = {
        'invalid_events': quality[0] or 0,
        'invalid_surfaces': quality[1] or 0,
        'invalid_media_types': quality[2] or 0,
        'invalid_media_ids': quality[3] or 0,
        'null_timestamps': quality[4] or 0,
    }

    # 11 — duplicate non-impression daily events (report, never mutate)
    dims = (RF.user_id, RF.media_id, RF.media_type, RF.surface, RF.event,
            RF.event_date)
    dupes = (
        RF.query.with_entities(*dims, _db.func.count(RF.id).label('n'))
        .filter(RF.event != 'impression')
        .group_by(*dims)
        .having(_db.func.count(RF.id) > 1)
        .all())
    agg['duplicate_groups'] = len(dupes)
    agg['duplicate_rows'] = sum(g[-1] for g in dupes)

    # 12 — all-time row count for context
    agg['total_all_time'] = RF.query.count()

    return agg


# ════════════════════════════════════════════════════════════════════════════
# Report rendering (pure — deterministic output ordering everywhere)
# ════════════════════════════════════════════════════════════════════════════

def _event_count(agg, event):
    return agg['events'].get(event, 0)


def _section(agg, title):
    logger.info('[%s]', title)


def _two_col(name, value):
    logger.info('%s: %s', name, value)


def _report_summary(agg, impressions, clicks):
    logger.info('[INFO] Window: %s days', agg['days'])
    _two_col('Events', agg['total_events'])
    _two_col('Impressions', impressions)
    _two_col('Clicks', clicks)
    _two_col('CTR', _ctr(clicks, impressions))
    _two_col('Active users', agg['active_users'])
    _two_col('Distinct titles', agg['distinct_titles'])
    if agg['active_users']:
        _two_col('Impressions per active user',
                 round(impressions / agg['active_users'], 2))
        _two_col('Clicks per active user',
                 round(clicks / agg['active_users'], 2))


def _report_events(agg):
    """All six event categories always appear (stable output §4)."""
    _section(agg, 'EVENTS')
    for event in RF_EVENTS:
        _two_col(event, _event_count(agg, event))


def _report_surfaces(agg):
    """Known surfaces always appear; unknown ones are quality warnings."""
    _section(agg, 'SURFACES')
    for surface in RF_SURFACES:
        counts = agg['surfaces'].get(surface, {})
        s_imp = counts.get('impression', 0)
        s_clk = counts.get('click', 0)
        logger.info('%s: impressions=%s clicks=%s ctr=%s',
                    surface, s_imp, s_clk, _ctr(s_clk, s_imp))
    for surface in sorted(set(agg['surfaces']) - set(RF_SURFACES)):
        logger.info('UNKNOWN_SURFACE %s: %s events', surface,
                    sum(agg['surfaces'][surface].values()))


def _report_positions(agg):
    """Position table 1–14, stable zero-filled rows (spec §13)."""
    _section(agg, 'POSITION')
    total_imp = total_clk = 0
    for position in range(1, MAX_POSITION + 1):
        counts = agg['positions'].get(position, {})
        p_imp = counts.get('impression', 0)
        p_clk = counts.get('click', 0)
        total_imp += p_imp
        total_clk += p_clk
        logger.info('position %s: impressions=%s clicks=%s ctr=%s',
                    position, p_imp, p_clk, _ctr(p_clk, p_imp))
    oor = agg['positions'].get(POSITION_OUT_OF_RANGE, {})
    if oor:
        logger.info('%s: impressions=%s clicks=%s', POSITION_OUT_OF_RANGE,
                    oor.get('impression', 0), oor.get('click', 0))
    _two_col('Average impression position',
             'n/a' if not total_imp else round(
                 sum(p * c.get('impression', 0)
                     for p, c in agg['positions'].items()
                     if isinstance(p, int)) / total_imp, 2))
    _two_col('Average click position',
             'n/a' if not total_clk else round(
                 sum(p * c.get('click', 0)
                     for p, c in agg['positions'].items()
                     if isinstance(p, int)) / total_clk, 2))


def _report_reasons(agg):
    """Top reasons by impressions, deterministic (-impressions, name)."""
    _section(agg, 'REASONS')
    ranked = sorted(
        agg['reasons'].items(),
        key=lambda kv: (-kv[1].get('impression', 0), kv[0]))[:TOP_REASONS]
    for reason, counts in ranked:
        r_imp = counts.get('impression', 0)
        r_clk = counts.get('click', 0)
        logger.info('%s: impressions=%s clicks=%s ctr=%s',
                    reason, r_imp, r_clk, _ctr(r_clk, r_imp))


def _report_sources(agg):
    """Top sources by impressions, stored verbatim, deterministic."""
    _section(agg, 'SOURCES')
    ranked = sorted(
        agg['sources'].items(),
        key=lambda kv: (-kv[1].get('impression', 0), kv[0]))[:TOP_SOURCES]
    for source, counts in ranked:
        s_imp = counts.get('impression', 0)
        s_clk = counts.get('click', 0)
        logger.info('%s: impressions=%s clicks=%s ctr=%s',
                    source, s_imp, s_clk, _ctr(s_clk, s_imp))


def _report_media(agg):
    """Known media types always appear (stable output §9)."""
    _section(agg, 'MEDIA')
    for media_type in RF_MEDIA_TYPES:
        counts = agg['media'].get(media_type, {})
        m_imp = counts.get('impression', 0)
        m_clk = counts.get('click', 0)
        logger.info('%s: impressions=%s clicks=%s ctr=%s',
                    media_type, m_imp, m_clk, _ctr(m_clk, m_imp))


def _report_users_and_funnel(agg, impressions, clicks):
    _section(agg, 'USERS')
    _two_col('Active users', agg['active_users'])
    _two_col('Users with impressions', agg['users_with_impressions'])
    _two_col('Users with clicks', agg['users_with_clicks'])
    _two_col('Users with both impressions and clicks',
             agg['users_with_both'])

    # Descriptive event-count funnel (neutral terminology — §11).
    _section(agg, 'FUNNEL')
    _two_col('impression event counts', impressions)
    _two_col('click event counts', clicks)
    _two_col('saved event counts', _event_count(agg, 'saved'))
    _two_col('rated event counts', _event_count(agg, 'rated'))
    _two_col('not_interested event counts',
             _event_count(agg, 'not_interested'))
    _two_col('already_watched event counts',
             _event_count(agg, 'already_watched'))

    # Effectiveness rates against impressions (§12).
    _section(agg, 'RATES')
    for event in ('click', 'saved', 'rated', 'not_interested',
                  'already_watched'):
        _two_col(f'{event} / impression',
                 _ctr(_event_count(agg, event), impressions))


def _report_trends(agg):
    """Daily trend, zero-filled, oldest first, bounded by the window (§14)."""
    _section(agg, 'TRENDS')
    for day in _window_dates(agg['days'])[:MAX_DAILY_ROWS]:
        counts = agg['daily'].get(day, {})
        d_imp = counts.get('impression', 0)
        d_clk = counts.get('click', 0)
        logger.info('%s: impressions=%s clicks=%s ctr=%s',
                    day.isoformat(), d_imp, d_clk, _ctr(d_clk, d_imp))


def _report_quality(agg):
    """Data-quality counts (§15/§16) — reported, never repaired.

    Returns 1 when any inconsistency exists, else 0."""
    _section(agg, 'QUALITY')
    quality = dict(agg['quality'])
    quality['duplicate_non_impressions'] = agg['duplicate_rows']
    for key in sorted(quality):
        logger.info('%s=%s', key, quality[key])
    return 0 if not any(quality.values()) else 1


def build_report(agg):
    """Render the full operator report (pure; deterministic ordering)."""
    impressions = _event_count(agg, 'impression')
    clicks = _event_count(agg, 'click')
    _report_summary(agg, impressions, clicks)
    _report_events(agg)
    _report_surfaces(agg)
    _report_positions(agg)
    _report_reasons(agg)
    _report_sources(agg)
    _report_media(agg)
    _report_users_and_funnel(agg, impressions, clicks)
    _report_trends(agg)
    return _report_quality(agg)


# ════════════════════════════════════════════════════════════════════════════
# Entry point
# ════════════════════════════════════════════════════════════════════════════

def _parse_days(raw):
    """argparse type for --days: integer within [MIN_DAYS, MAX_DAYS]."""
    try:
        days = int(raw)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError(
            f'days must be an integer between {MIN_DAYS} and {MAX_DAYS}')
    if days < MIN_DAYS or days > MAX_DAYS:
        raise argparse.ArgumentTypeError(
            f'days must be between {MIN_DAYS} and {MAX_DAYS}')
    return days


def run(days=DEFAULT_DAYS):
    """Collect aggregates, print the report, return the exit code (0/1).
    Fatal errors propagate to main() for exit 2."""
    logger.info('[START] Recommendation feedback analytics')
    agg = collect(days)
    issues = build_report(agg)

    if agg['duplicate_groups']:
        issues = 1
        logger.warning(
            '[WARN] duplicate non-impression events: %s rows in %s groups '
            '(expected: impossible while the '
            'uq_recommendation_feedback_event_daily partial unique index '
            'exists)', agg['duplicate_rows'], agg['duplicate_groups'])
    if issues:
        logger.warning('[STATUS] INCONSISTENT')
        return 1
    logger.info('[STATUS] OK')
    return 0


def main(argv=None):
    logging.basicConfig(
        level=logging.INFO,
        format="%(message)s",   # operator-facing: concise, no logger prefix
        stream=sys.stdout)      # spec §18: report on stdout (logs merge stderr)
    parser = argparse.ArgumentParser(
        description='Read-only recommendation feedback analytics '
                    f'(window {MIN_DAYS}–{MAX_DAYS} days, '
                    f'default {DEFAULT_DAYS}).')
    parser.add_argument('--days', type=_parse_days, default=DEFAULT_DAYS,
                        help=f'analysis window in days '
                             f'({MIN_DAYS}–{MAX_DAYS}, '
                             f'default {DEFAULT_DAYS})')
    args = parser.parse_args(argv)

    try:
        _load()
        with _app.app_context():
            return run(days=args.days)
    except Exception as exc:  # noqa: BLE001 — operator-facing fatal boundary
        logger.error("[FATAL] %s: %s", type(exc).__name__, exc)
        return 2


if __name__ == '__main__':
    sys.exit(main())
