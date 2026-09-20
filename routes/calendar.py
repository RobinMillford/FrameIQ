"""Unified calendar API + page (Feature 10).

GET /api/calendar — authenticated, user-scoped, bounded date range,
deterministic ordering. Parameters:

    ?start=YYYY-MM-DD   (default: today-7d)
    ?end=YYYY-MM-DD     (default: today+30d, hard-capped at +62d)
    &type=all|tv|movie
    &scope=all|watchlist|tracking

No user_id parameter exists: the calendar is always the current
session user's. Invalid dates are rejected with 400 rather than
silently ignored. The movie-release region (Feature 10B) is the
caller's own saved streaming_region — never a request parameter.
"""
import logging
from datetime import datetime

from flask import Blueprint, jsonify, render_template, request
from flask_login import current_user, login_required

from api.availability import normalize_region
from api.calendar import (MAX_RANGE_DAYS, clamp_range, default_range,
                          get_calendar_events)

logger = logging.getLogger(__name__)

calendar_bp = Blueprint('calendar', __name__)


@calendar_bp.route('/calendar')
@login_required
def calendar_page():
    """Unified personal entertainment calendar page."""
    return render_template('calendar.html')


@calendar_bp.route('/api/calendar', methods=['GET'])
@login_required
def api_calendar():
    """Unified calendar events for the current user."""
    today = datetime.utcnow().date()

    raw_start = request.args.get('start')
    raw_end = request.args.get('end')
    try:
        if raw_start:
            start = datetime.strptime(raw_start, '%Y-%m-%d').date()
        else:
            start, end = default_range(today)
        if raw_end:
            end = datetime.strptime(raw_end, '%Y-%m-%d').date()
        elif raw_start:
            end = start  # explicit start without end → single day
    except ValueError:
        return jsonify({'error': 'Invalid date format; expected YYYY-MM-DD'}), 400

    if end < start:
        return jsonify({'error': 'end must be on or after start'}), 400

    start, end, capped = clamp_range(start, end)
    if start > today and not raw_start:
        # default range always includes today
        start = today

    event_type = request.args.get('type', 'all')
    scope = request.args.get('scope', 'all')

    # Feature 10B: movie release events resolve against the caller's own
    # saved region (the canonical streaming_region preference). There is
    # deliberately NO ?region= parameter — a caller cannot probe another
    # region or another user's release calendar through this endpoint.
    region = normalize_region(getattr(current_user, 'streaming_region', None))

    try:
        events, meta = get_calendar_events(
            current_user.id, start, end,
            event_type=event_type, scope=scope, region=region)
    except ValueError as exc:
        return jsonify({'error': str(exc)}), 400

    meta['range_capped'] = capped
    meta['max_range_days'] = MAX_RANGE_DAYS
    return jsonify({
        'events': events,
        'meta': meta,
    }), 200
