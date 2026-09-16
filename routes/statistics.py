"""Personal statistics API (Feature #8, Phase 3) — private read-only adapter.

GET /api/statistics exposes the authenticated user's own canonical viewing
statistics (Feature #8 Phase 2 service) as JSON. The route is an ADAPTER
around api.statistics.get_statistics(): it adds

- authentication (flask-login) — anonymous users get redirected/401; the
  session user is authoritative and the ONLY identity input. There is
  deliberately no user_id/username/email parameter, so reading another
  user's statistics is impossible by construction
- query-parameter parsing for the Phase 2 window contract:

      /api/statistics               → current calendar year
      /api/statistics?year=2025     → the 2025 calendar year
      /api/statistics?lifetime=true → all history

  and nothing else. Arbitrary start/end query parameters are NOT exposed.
- clean 400 responses for malformed input (never a traceback):
  non-integer years, out-of-range years (canonical service validation),
  and the year + lifetime combination, which the canonical service would
  silently resolve (lifetime wins) — the API rejects it explicitly
  instead of guessing

The response body is the canonical service's presentation-safe dict
(no database IDs, no ORM objects, no debug fields) plus the resolved
"period" for UI labeling. Nothing is computed, cached, or mutated here,
and the service performs zero network calls.
"""
import logging

from datetime import datetime

from flask import Blueprint, jsonify, request
from flask_login import login_required, current_user

from extensions import limiter
import api.statistics as statistics_service

logger = logging.getLogger(__name__)

statistics_bp = Blueprint('statistics', __name__)

# Read-only adapter over 5 bounded SQL statements.
RATE_LIMIT = "60 per minute"

_LIFETIME_TRUE = ('true', '1', 'yes')
_LIFETIME_FALSE = ('false', '0', 'no')


def _parse_window_params():
    """Parse ?year= / ?lifetime= into the service window arguments.

    Returns (error_response_or_None, lifetime_or_None, year_or_None).
    Both params absent → (None, None, None): the service's current-year
    default applies.
    """
    raw_year = request.args.get('year')
    raw_lifetime = request.args.get('lifetime')

    lifetime = None
    if raw_lifetime is not None:
        value = raw_lifetime.strip().lower()
        if value in _LIFETIME_TRUE:
            lifetime = True
        elif value in _LIFETIME_FALSE:
            lifetime = False
        else:
            return (jsonify(
                {'error': "lifetime must be 'true' or 'false'"}), 400), \
                None, None

    year = None
    if raw_year is not None:
        try:
            year = int(raw_year.strip())
        except (ValueError, TypeError):
            return (jsonify({'error': 'year must be an integer'}), 400), \
                None, None

    if year is not None and lifetime:
        # The canonical service resolves this silently (lifetime wins);
        # the API refuses the ambiguity instead of picking a side.
        return (jsonify(
            {'error': 'year and lifetime are mutually exclusive'}), 400), \
            None, None

    return None, lifetime, year


@statistics_bp.route('/api/statistics')
@login_required
@limiter.limit(RATE_LIMIT)
def api_statistics():
    """The authenticated user's own viewing statistics (canonical service)."""
    error, lifetime, year = _parse_window_params()
    if error is not None:
        return error

    try:
        if lifetime:
            stats = statistics_service.get_statistics(
                current_user.id, lifetime=True)
        else:
            stats = statistics_service.get_statistics(
                current_user.id, year=year)
    except ValueError as exc:
        # Canonical service validation (malformed / out-of-range years).
        return jsonify({'error': str(exc)}), 400
    except Exception:
        logger.error("Statistics load failed for user %s",
                     current_user.id, exc_info=True)
        return jsonify({'error': 'Internal server error'}), 500

    if lifetime:
        period = {'type': 'lifetime', 'year': None}
    else:
        period = {'type': 'year',
                  'year': year if year is not None
                  else datetime.now().year}
    return jsonify({'period': period, **stats})
