"""Batched user view-state endpoint (Task C — cross-surface sync).

GET /api/view-state?movies=1,2&tv=10,11

One private, user-scoped JSON payload describing the CURRENT user's
canonical viewing state for a page-scoped id list:

    {"viewed_movie_ids": [...], "tv_progress": {show_id: {...}}}

Consumed by the client store (static/js/view_state_client.js) for every
client-rendered media surface: browse, trending, CineBot cards, For You.
Surfaces never call this per card — one call per page render / re-render.

Privacy + caching contract (Phase 6/20):
  - @login_required: anonymous users get no personalized state at all
  - Cache-Control: no-store — a personalized response must never be
    replayed by a browser or CDN for another user
  - ids are page-scoped (bounded IN lists); no all-history reads
"""
import logging

from flask import jsonify, request
from flask_login import current_user, login_required

from extensions import limiter
from routes._main_bp import main
from api.user_view_state import view_state_payload

logger = logging.getLogger(__name__)

# Bounded id lists: a page carries tens of cards, not thousands. The cap
# simply rejects pathological manual calls — no legit surface needs more.
MAX_IDS = 200


def _parse_ids(raw):
    """Comma-separated integer ids → bounded, deduped list (None→[])."""
    if not raw:
        return []
    ids, seen = [], set()
    for chunk in raw.split(",")[:MAX_IDS]:
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            value = int(chunk)
        except ValueError:
            return None  # malformed → 400
        if value > 0 and value not in seen:
            seen.add(value)
            ids.append(value)
    return ids


@main.route('/api/view-state', methods=['GET'])
@login_required
@limiter.limit("60 per minute")
def get_view_state():
    """Current user's viewed movies + TV progress for page-scoped ids."""
    movie_ids = _parse_ids(request.args.get('movies', ''))
    tv_ids = _parse_ids(request.args.get('tv', ''))
    if movie_ids is None or tv_ids is None:
        return jsonify({'error': 'ids must be a comma-separated '
                                 'list of positive integers'}), 400
    try:
        payload = view_state_payload(current_user, movie_ids, tv_ids)
    except Exception:
        logger.error("view-state payload failed for user %s",
                     current_user.id, exc_info=True)
        return jsonify({'error': 'An unexpected error occurred'}), 500
    response = jsonify(payload)
    response.headers['Cache-Control'] = 'no-store'
    return response
