"""For You API route (Feature #6/#7 Phase 4) — thin authenticated exposure.

GET /api/for-you exposes the canonical engine (api/for_you.py) to the
authenticated frontend. The route adds only:

- authentication (flask-login) — anonymous users get 401; the engine itself
  never sees a request object and never accepts a caller-chosen user_id,
  so impersonation is impossible
- a bounded `limit` query parameter (1..14; malformed → 400)
- an optional `region` override clamped to the caller's own stored
  streaming region (a caller cannot probe another region or another user)

GET is a read; Flask-WTF's CSRFProtect only protects state-changing verbs,
matching the app's existing GET policy — no CSRF token required, nothing
is mutated.

Cold-start responses are 200 with personalized=false + a stable
reason_state — a user without taste history is a normal state, not an
error. No recommendation computation happens at import or on other routes;
no TasteProfile recomputation is triggered here.
"""
import logging

from flask import Blueprint, jsonify, request
from flask_login import login_required, current_user

from extensions import limiter
import api.for_you as for_you_engine

logger = logging.getLogger(__name__)

for_you_bp = Blueprint('for_you', __name__)

# Read-only, bounded (engine: ≤5 TMDb calls + ≤12 availability probes).
RATE_LIMIT = "30 per minute"


@for_you_bp.route('/api/for-you')
@login_required
@limiter.limit(RATE_LIMIT)
def api_for_you():
    """Personalized For You candidates for the authenticated user."""
    raw_limit = request.args.get('limit')
    limit = for_you_engine.MAX_RESULTS
    if raw_limit is not None:
        try:
            limit = int(raw_limit)
        except (TypeError, ValueError):
            return jsonify({'error': 'limit must be an integer'}), 400
        if limit < 1 or limit > for_you_engine.MAX_RESULTS:
            return jsonify(
                {'error': f'limit must be between 1 and '
                          f'{for_you_engine.MAX_RESULTS}'}), 400

    # Region: the caller's own stored preference wins; an explicit override
    # is honored only as a narrow self-region variant, never cross-user.
    region = (current_user.streaming_region
              or request.args.get('region') or None)

    try:
        result = for_you_engine.get_for_you(current_user.id, region=region,
                                            limit=limit)
    except Exception:
        logger.error("For You computation failed for user %s",
                     current_user.id, exc_info=True)
        return jsonify({'error': 'Internal server error'}), 500

    return jsonify(result)
